"""小月 QQ 接入插件 v1.3（借壳小白，第 8 课）。

路由规则（隐私优先）：
- 群聊：一律静默（should_call_llm(True) 禁止默认 LLM，零个人信息暴露）
- 私聊：仅主人 QQ（owner_qq）→ 直调小月服务 /api/chat，直接 event.send 回复
- 陌生私聊：静默

v1.3 新增：主人发文件 → 自动入库知识库
- 支持 .txt/.md/.csv（直读）与 .docx/.pdf（解析提取）
- 大小上限 2MB；同名文件覆盖旧版（ingest 按 doc_name 幂等）
- 仅主人私聊可用（should_handle 白名单，与文本消息同闸门）
- 临时文件入库后即删

v1.1 修复（相对 v1.0）：
- 用 @filter.event_message_type(ALL) 消息处理器而不是 on_llm_request——
  AstrBot v4.27 的 on_llm_request 钩子跑在 LLM 门控之后，拦不住默认 LLM；
  消息处理器在 ProcessStage 门控之前执行，拦得住
- should_call_llm 语义：True=禁止默认 LLM（v1.0 用 False 是反的）
- 回复走 event.send()（置 _has_send_oper，双保险跳过默认 LLM）
"""
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
FILE_MAX_BYTES = 2 * 1024 * 1024  # 文件入库上限 2MB（切块成本+内存保护）
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".log", ".json"}
# 文本提取失败时的安全文件名（防路径穿越/非法字符）
_SAFE_NAME = re.compile(r'[\\/:*?"<>|]')


def should_handle(sender: str, group: str, owner_qq: str) -> bool:
    """白名单判定（纯函数，可单测）。

    规则：群聊一律不处理（隐私铁律）；owner 未配置=全拒（fail-closed）；
    仅主人私聊返回 True。
    """
    if group:
        return False
    owner = str(owner_qq or "").strip()
    if not owner or str(sender or "").strip() != owner:
        return False
    return True


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
    "小月 QQ 接入（借壳小白）：主人私聊直达小月服务，群聊/陌生人静默",
    "v1.3.0",
)
class XiaoYuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config or {}
        # trust_env=False：本机回环调用不走系统代理——宿主机若有 HTTP_PROXY
        # 且 NO_PROXY 不含 127.0.0.1，Bearer token 会流经代理（全套已踩过的坑）
        self._client = httpx.AsyncClient(timeout=120, trust_env=False)  # 小月 LLM 回复可能 30-60s
        # QQ 文件 CDN 对 JD 直连常 502（直连出网受限），下载兜底走本机 clash
        self._proxy_client = httpx.AsyncClient(
            timeout=120, trust_env=False, proxy="http://127.0.0.1:7890"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg = event.get_message_str() or ""
        sender = event.get_sender_id() or ""
        group = event.get_group_id() or ""

        # 白名单（纯函数）：群聊不处理；仅主人私聊放行
        if not should_handle(sender, group, self.cfg.get("owner_qq", "")):
            event.should_call_llm(True)
            event.stop_event()
            return
        event.stop_event()  # 主人消息本插件全权处理，阻断其他处理器

        # 文件分支（仅主人）：识别 File 组件 → 提取文本 → 入库知识库
        file_comp = self._find_file_component(event)
        if file_comp is not None:
            await self._handle_file(event, file_comp)
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
            r = await self._client.post(
                f"{base}/api/chat",
                json={"message": msg.strip()},
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
        """下载 QQ 文件 URL：直连优先，失败走 clash 代理兜底。"""
        for label, client in (("direct", self._client), ("proxy", self._proxy_client)):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                Path(dest).write_bytes(resp.content)
                return True
            except Exception as e:
                logger.warning(f"[xy] 文件下载失败({label}): {type(e).__name__}: {e}")
        return False

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
        # 优先拿原始 URL 自己下载（AstrBot 内置下载器直连 QQ CDN 常 502，
        # 且同步访问 .file 在异步上下文会卡死）——直连失败自动走 clash 代理
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
        tmp_path = ""
        try:
            # ① 拿到本地文件（已落盘 / URL 下载）
            if src.startswith(("http://", "https://")):
                tmp_path = str(Path(os.environ.get("TEMP", "/tmp")) / f"xy_ingest_{os.getpid()}_{name}")
                if not await self._download(src, tmp_path):
                    await event.send(MessageChain([Plain(
                        f"❌ 文件「{name}」下载失败（QQ 文件链接直连和代理都失败），稍后再发一次试试"
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
                await event.send(MessageChain([Plain(f"❌ 文件「{name}」{size / 1024 / 1024:.1f}MB 超过上限 2MB，请拆分后再发")]))
                return

            # ③ 提取文本
            text, err = extract_text(local)
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
