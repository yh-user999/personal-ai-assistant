"""小月 QQ 接入插件 v1.1（借壳小白，第 8 课）。

路由规则（隐私优先）：
- 群聊：一律静默（should_call_llm(True) 禁止默认 LLM，零个人信息暴露）
- 私聊：仅主人 QQ（owner_qq）→ 直调小月服务 /api/chat，直接 event.send 回复
- 陌生私聊：静默

v1.1 修复（相对 v1.0）：
- 用 @filter.event_message_type(ALL) 消息处理器而不是 on_llm_request——
  AstrBot v4.27 的 on_llm_request 钩子跑在 LLM 门控之后，拦不住默认 LLM；
  消息处理器在 ProcessStage 门控之前执行，拦得住
- should_call_llm 语义：True=禁止默认 LLM（v1.0 用 False 是反的）
- 回复走 event.send()（置 _has_send_oper，双保险跳过默认 LLM）
"""
import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.core.message.message_event_result import MessageChain

REPLY_MAX_CHARS = 4000  # QQ 单条消息安全长度，超长截断并提示


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


@register(
    "astrbot_plugin_xy",
    "小月接入",
    "小月 QQ 接入（借壳小白）：主人私聊直达小月服务，群聊/陌生人静默",
    "v1.2.0",
)
class XiaoYuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config or {}
        # trust_env=False：本机回环调用不走系统代理——宿主机若有 HTTP_PROXY
        # 且 NO_PROXY 不含 127.0.0.1，Bearer token 会流经代理（全套已踩过的坑）
        self._client = httpx.AsyncClient(timeout=120, trust_env=False)  # 小月 LLM 回复可能 30-60s

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
        if not msg.strip():
            # 空白消息同样拦默认 LLM（v1.2：漏拦会漏进宿主默认 LLM）
            event.should_call_llm(True)
            event.stop_event()
            return
        event.stop_event()  # 主人消息本插件全权处理，阻断其他处理器

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
