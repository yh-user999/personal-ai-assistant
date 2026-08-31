"""小月 QQ 接入插件 v1.4（借壳小白，第 8 课 + 第 9 课多人支持）。

路由规则（隐私优先）：
- 群聊：一律静默（should_call_llm(True) 禁止默认 LLM，零个人信息暴露）
- 私聊：任何 QQ 用户都能聊（v1.4 多人支持）——透传 sender QQ 号给
  小月服务 /api/chat，服务端按 QQ 号完全隔离记忆
- 陌生私聊：可聊，但仅限对话；主人专属功能（执行器/提醒/文件入库等）只在主人会话生效
- 文件入库：仅主人私聊可用（should_handle 白名单，与 v1.3 相同）

v1.4 多人支持：
- /api/chat 请求带 user_id=sender（QQ 号），小月按人隔离记忆/事实/目标
- 群聊静默、owner 未配置 fail-closed 不变；访客不 stop_event（不挡其他插件）

v1.3 新增：主人发文件 → 自动入库知识库
- 支持 .txt/.md/.csv（直读）与 .docx/.pdf（解析提取）
- 大小上限 10MB；同名文件覆盖旧版（ingest 按 doc_name 幂等）
- 仅主人私聊可用（should_handle 白名单，与文本消息同闸门）
- 临时文件入库后即删

v1.1 修复（相对 v1.0）：
- 用 @filter.event_message_type(ALL) 消息处理器而不是 on_llm_request——
  AstrBot v4.27 的 on_llm_request 钩子跑在 LLM 门控之后，拦不住默认 LLM；
  消息处理器在 ProcessStage 门控之前执行，拦得住
- should_call_llm 语义：True=禁止默认 LLM（v1.0 用 False 是反的）
- 回复走 event.send()（置 _has_send_oper，双保险跳过默认 LLM）
"""
import asyncio
import os
import re
from pathlib import Path

import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

REPLY_MAX_CHARS = 4000  # QQ 单条消息安全长度，超长截断并提示
FILE_MAX_BYTES = 10 * 1024 * 1024  # 文件入库上限 10MB（图文 PDF 常超 2MB，文本提取成本低）
FILE_TOTAL_TIMEOUT = 35  # 单个文件从取回到入库的总预算，避免 QQ 长时间无回复
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".log", ".json"}


class FileTooLargeError(Exception):
    """NapCat 已提供文件大小，尚未下载就超过入库上限。"""
# 文本提取失败时的安全文件名（防路径穿越/非法字符）
_SAFE_NAME = re.compile(r'[\\/:*?"<>|]')


def find_file_id(history_messages: list, name: str) -> tuple[str, int | None] | None:
    """按文件名精确找最近一条 file_id 和大小；找不到绝不猜其他文件。"""
    for m in reversed(history_messages):
        for seg in m.get("message") or []:
            if seg.get("type") != "file":
                continue
            data = seg.get("data") or {}
            if data.get("file") != name:
                continue
            fid = data.get("file_id")
            if not fid:
                return None
            try:
                size = int(data.get("file_size")) if data.get("file_size") else None
            except (TypeError, ValueError):
                size = None
            return str(fid), size
    return None


# NapCat 容器路径 → 宿主机路径映射的默认值（可被 container_path_map 配置覆盖）
_DEFAULT_PATH_MAP = (
    ("/app/.config/QQ/", "/opt/napcat/qq_config/"),
    ("/app/napcat/cache/", "/opt/napcat/cache/"),
    ("/app/napcat/config/", "/opt/napcat/config/"),
)


def _parse_path_map(cfg_value: str) -> tuple:
    """配置串 '容器前缀=宿主机前缀;...' → 映射元组；空/格式错回落默认。"""
    if not str(cfg_value or "").strip():
        return _DEFAULT_PATH_MAP
    pairs = []
    for seg in str(cfg_value).split(";"):
        if "=" in seg:
            cont, host = seg.split("=", 1)
            if cont.strip() and host.strip():
                pairs.append((cont.strip(), host.strip()))
    return tuple(pairs) if pairs else _DEFAULT_PATH_MAP


def to_host_path(p: str, path_map=_DEFAULT_PATH_MAP) -> str:
    """容器内路径翻译为宿主机路径（挂载点映射）；非容器路径原样返回。"""
    for cont, host in path_map:
        if p.startswith(cont):
            return host + p[len(cont):]
    return p


def should_handle(sender: str, group: str, owner_qq: str) -> bool:
    """主人专属闸门（纯函数，可单测）——文件入库等主人功能用。

    规则：群聊一律不处理（隐私铁律）；owner 未配置=全拒（fail-closed）；
    仅主人私聊返回 True。
    """
    if group:
        return False
    owner = str(owner_qq or "").strip()
    if not owner or str(sender or "").strip() != owner:
        return False
    return True


def can_chat(sender: str, group: str, owner_qq: str) -> bool:
    """聊天放行判定（v1.4 多人支持，纯函数，可单测）。

    规则：群聊一律不处理；owner 未配置=全拒（fail-closed，与 v1.3 一致）；
    私聊（含陌生人）一律放行——小月服务端按 sender QQ 号隔离记忆。
    """
    if group:
        return False
    if not str(owner_qq or "").strip():
        return False  # owner 未配置：无法区分主人/文件权限，宁可全拒
    return bool(str(sender or "").strip())


def extract_text(path: str) -> tuple[str, str]:
    """从文件提取纯文本。返回 (文本, 错误信息)，成功时错误信息为空。

    支持：txt/md/csv/log/json 直读；docx（python-docx）；pdf（pypdf）。
    """
    ext = Path(path).suffix.lower()
    try:
        if ext in TEXT_EXTS:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            return text, ""
        if ext == ".docx":
            from docx import Document  # python-docx，服务器同款依赖

            doc = Document(path)
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:  # 表格文本也要（设定卡常见表格）
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        paras.append(" | ".join(cells))
            return "\n".join(paras), ""
        if ext == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            pages = [(page.extract_text() or "") for page in reader.pages]
            return "\n".join(pages), ""
        return "", f"暂不支持 {ext or '无扩展名'} 格式（支持 txt/md/csv/docx/pdf）"
    except Exception as e:
        return "", f"解析失败：{type(e).__name__}: {e}"


def safe_doc_name(name: str) -> str:
    """文件名 → 安全文档名（去路径成分与非法字符，防穿越）。"""
    # QQ 端文件名可能携带 Windows 反斜杠路径（..\\..\\evil.txt），
    # 先统一成 / 再取 basename——Linux 上 Path().name 不认反斜杠分隔符
    name = name.replace("\\", "/")
    name = Path(name).name  # 去目录成分
    return _SAFE_NAME.sub("_", name).strip() or "未命名文档"


@register(
    "astrbot_plugin_xy",
    "小月接入",
    "小月 QQ 接入（借壳小白）：私聊直达小月服务（多人按 QQ 号隔离记忆），群聊静默",
    "v1.4.0",
)
class XiaoYuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config or {}
        # trust_env=False：本机回环调用不走系统代理——宿主机若有 HTTP_PROXY
        # 且 NO_PROXY 不含 127.0.0.1，Bearer token 会流经代理（全套已踩过的坑）
        self._client = httpx.AsyncClient(timeout=120, trust_env=False)  # 小月 LLM 回复可能 30-60s
        # QQ 文件 CDN 直连常 502（出网受限），下载兜底走代理（可配置，默认 clash）
        proxy = str(self.cfg.get("download_proxy", "") or "").strip()
        self._proxy_client = httpx.AsyncClient(
            timeout=120, trust_env=False, proxy=proxy or "http://127.0.0.1:7890"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg = event.get_message_str() or ""
        sender = event.get_sender_id() or ""
        group = event.get_group_id() or ""
        owner = str(self.cfg.get("owner_qq", "") or "").strip()

        # 群聊一律静默（隐私铁律）；私聊放行闸门（v1.4：陌生人可聊）
        if not can_chat(sender, group, owner):
            # 仅禁止默认 LLM；不 stop_event，避免影响 meme_manager 等其他插件的事件处理。
            event.should_call_llm(True)
            return
        # 主人消息本插件全权处理，阻断其他处理器；访客不 stop_event（留给其他插件）
        if sender == owner:
            event.stop_event()

        # 文件分支（仅主人）：识别 File 组件 → 提取文本 → 入库知识库
        if should_handle(sender, group, owner):
            file_comp = self._find_file_component(event)
            if file_comp is not None:
                try:
                    await asyncio.wait_for(
                        self._handle_file(event, file_comp), timeout=FILE_TOTAL_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.warning("[xy] 文件处理超过总时限 %ss", FILE_TOTAL_TIMEOUT)
                    await event.send(MessageChain([Plain("❌ 文件处理超时（35秒），请稍后重发或先压缩文件")]))
                return

        if not msg.strip():
            # 空白消息同样拦默认 LLM（v1.2：漏拦会漏进宿主默认 LLM）
            event.should_call_llm(True)
            return

        base = str(self.cfg.get("api_base", "") or "").strip().rstrip("/")
        token = str(self.cfg.get("api_token", "") or "").strip()
        if not base or not token:
            logger.error("[xy] 插件配置缺失（api_base/api_token 为空），请到 AstrBot 控制台配置")
            event.should_call_llm(True)
            return

        try:
            # v1.4：透传 sender QQ 号——小月按人隔离记忆；空/非法值由服务端 400
            r = await self._client.post(
                f"{base}/api/chat",
                json={"message": msg.strip(), "user_id": sender},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            reply = r.json().get("reply", "") or ""
        except Exception as e:
            logger.warning(f"[xy] 小月服务调用失败: {type(e).__name__}: {e}")
            reply = "😅 小月服务暂时不可达（服务器在重启？），稍后再试"

        reply = reply.strip()
        if len(reply) > REPLY_MAX_CHARS:
            reply = reply[:REPLY_MAX_CHARS] + "\n…（内容过长已截断，完整版去电脑面板看）"

        # 直接发送并禁止默认 LLM（_has_send_oper 双保险）
        await event.send(MessageChain([Plain(reply)]))
        event.should_call_llm(True)

    async def terminate(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
        try:
            await self._proxy_client.aclose()
        except Exception:
            pass

    async def _download(self, url: str, dest: str) -> bool:
        """下载 QQ 文件 URL：直连优先，失败走 clash 代理兜底（20s 短超时，别把管线拖死）。"""
        for label, client in (("direct", self._client), ("proxy", self._proxy_client)):
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                Path(dest).write_bytes(resp.content)
                return True
            except Exception as e:
                logger.warning(f"[xy] 文件下载失败({label}): {type(e).__name__}: {e}")
        return False

    async def _napcat_get_file(self, name: str) -> tuple[str, bool] | None:
        """NapCat 会话下载：历史 API 找 file_id → onebot get_file → 本地路径/base64。

        这是最可靠的通道——NapCat 用 QQ 会话取文件，不经公网 CDN（直连会 502/挂起）。
        返回本地文件路径；不可用返回 None。
        """
        ob_url = str(self.cfg.get("onebot_http", "") or "").strip().rstrip("/")
        ob_token = str(self.cfg.get("onebot_token", "") or "").strip()
        owner = str(self.cfg.get("owner_qq", "") or "").strip()
        if not ob_url or not owner:
            return None
        headers = {"Authorization": f"Bearer {ob_token}"} if ob_token else {}
        try:
            r = await self._client.post(
                f"{ob_url}/get_friend_msg_history",
                json={"user_id": int(owner), "count": 8},
                headers=headers,
                timeout=15,
            )
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") != "ok":
                return None
            msgs = (payload.get("data") or {}).get("messages") or []
            file_info = find_file_id(msgs, name)
            if not file_info:
                return None
            fid, declared_size = file_info
            if declared_size is not None and declared_size > FILE_MAX_BYTES:
                raise FileTooLargeError(
                    f"{declared_size / 1024 / 1024:.1f}MB 超过上限 {FILE_MAX_BYTES / 1024 / 1024:.0f}MB"
                )
            r2 = await self._client.post(
                f"{ob_url}/get_file",
                json={"file_id": fid},
                headers=headers,
                timeout=90,
            )
            r2.raise_for_status()
            j = r2.json()
            if j.get("status") != "ok":
                return None
            d = j.get("data") or {}
            if d.get("file"):
                host_path = to_host_path(
                    str(d["file"]),
                    _parse_path_map(self.cfg.get("container_path_map", "")),
                )
                if Path(host_path).exists():
                    return host_path, False
            if d.get("base64"):
                import base64

                dest = str(Path(os.environ.get("TEMP", "/tmp")) / f"xy_ingest_{os.getpid()}_{name}")
                Path(dest).write_bytes(base64.b64decode(d["base64"]))
                return dest, True
            return None
        except FileTooLargeError:
            raise
        except Exception as e:
            logger.warning(f"[xy] NapCat get_file 失败: {type(e).__name__}: {e}")
            return None

    # ── 文件入库（仅主人）─────────────────────────────────

    @staticmethod
    def _find_file_component(event: AstrMessageEvent):
        """遍历消息组件链找 File 组件；无则 None。

        兼容两种形态：本地已落盘（file=路径）与 URL 下发（file=http(s) 链接）。
        """
        try:
            from astrbot.api.message_components import File
        except ImportError:
            logger.warning("[xy] 当前 AstrBot 版本无 File 组件，文件入库不可用")
            return None
        for comp in event.get_messages() or []:
            if isinstance(comp, File):
                return comp
        return None

    async def _handle_file(self, event: AstrMessageEvent, comp) -> None:
        """文件 → 提取文本 → /api/knowledge/ingest → 回执。任何失败都给主人明确原因。"""
        name = safe_doc_name(getattr(comp, "name", "") or "未命名文档")
        tmp_path = ""
        try:
            # ① 拿到本地文件：优先 NapCat 会话下载（get_file，QQ 会话通道最可靠），
            #    失败退回 URL 自下载（直连→clash 代理），再退回已落盘路径
            try:
                file_result = await self._napcat_get_file(name)
            except FileTooLargeError as e:
                await event.send(MessageChain([Plain(f"❌ 文件「{name}」{e}，请拆分后再发")]))
                return
            local = file_result[0] if file_result else None
            if file_result and file_result[1]:
                tmp_path = local
            if not local:
                src = ""
                try:
                    getter = getattr(comp, "get_file", None)
                    if callable(getter):
                        src = str(await getter(allow_return_url=True) or "")
                    if not src:
                        src = str(getattr(comp, "url", "") or "")
                except Exception as e:
                    logger.warning(f"[xy] get_file 失败: {type(e).__name__}: {e}")
                    src = ""
                if src.startswith(("http://", "https://")):
                    tmp_path = str(Path(os.environ.get("TEMP", "/tmp")) / f"xy_ingest_{os.getpid()}_{name}")
                    if not await self._download(src, tmp_path):
                        await event.send(MessageChain([Plain(
                            f"❌ 文件「{name}」下载失败（NapCat 会话与直连/代理均失败），稍后再发一次试试"
                        )]))
                        return
                    local = tmp_path
                elif src and Path(src).exists():
                    local = src
                else:
                    await event.send(MessageChain([Plain(f"❌ 文件「{name}」未能落盘（ NapCat 未提供路径），换个方式再发一次试试")]))
                    return

            # ② 大小限制
            size = Path(local).stat().st_size
            if size > FILE_MAX_BYTES:
                await event.send(MessageChain([Plain(f"❌ 文件「{name}」{size / 1024 / 1024:.1f}MB 超过上限 10MB，请拆分后再发")]))
                return

            # ③ 提取文本
            # PDF/docx 解析是同步 CPU/IO，放到线程避免阻塞 AstrBot 事件循环。
            text, err = await asyncio.to_thread(extract_text, local)
            if err:
                await event.send(MessageChain([Plain(f"❌ 「{name}」{err}")]))
                return
            if not text.strip():
                await event.send(MessageChain([Plain(f"❌ 「{name}」没有提取到文本（扫描版 PDF/空文件？）")]))
                return

            # ④ 入库（同名覆盖）
            base = str(self.cfg.get("api_base", "") or "").strip().rstrip("/")
            token = str(self.cfg.get("api_token", "") or "").strip()
            if not base or not token:
                await event.send(MessageChain([Plain("❌ 插件配置缺失（api_base/api_token），请到 AstrBot 控制台配置")]))
                return
            r = await self._client.post(
                f"{base}/api/knowledge/ingest",
                json={"name": name, "content": text},
                headers={"Authorization": f"Bearer {token}"},
            )
            r.raise_for_status()
            chunks = r.json().get("chunks", 0)
            stem = Path(name).stem
            await event.send(MessageChain([Plain(
                f"📥 《{name}》已入库：{len(text)} 字，切了 {chunks} 块。\n"
                f"现在可以直接问我它的内容了（如「根据《{stem}》…」）"
            )]))
        except Exception as e:
            logger.warning(f"[xy] 文件入库失败 {name}: {type(e).__name__}: {e}")
            await event.send(MessageChain([Plain(f"❌ 「{name}」入库失败：{type(e).__name__}: {e}")]))
        finally:
            # 临时下载文件即删（源文件不动）
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except Exception:
                    pass
