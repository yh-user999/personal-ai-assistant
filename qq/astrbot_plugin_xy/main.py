"""小月 QQ 接入插件（借壳小白，第 8 课）。

路由规则（v1，隐私优先）：
- 群聊：一律静默（不调小月、不调 AstrBot LLM）——零个人信息暴露
- 私聊：仅主人 QQ（owner_qq）→ 直调小月服务 /api/chat，全功能
- 陌生私聊：静默（AstrBot 平台白名单之外的流量到不了这里，双保险）

小月服务返回的是小月自己的完整大脑（记忆/知识库/命令/情绪），
本插件不做任何人格加工。
"""
import httpx
from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

REPLY_MAX_CHARS = 4000  # QQ 单条消息安全长度，超长截断并提示


@register(
    "astrbot_plugin_xy",
    "小月接入",
    "小月 QQ 接入（借壳小白）：主人私聊直达小月服务，群聊/陌生人静默",
    "v1.0.0",
)
class XiaoYuePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.cfg = config or {}
        self._client = httpx.AsyncClient(timeout=120)  # 小月 LLM 回复可能 30-60s

    @filter.on_llm_request(priority=0)
    async def on_request(self, event: AstrMessageEvent, req=None):
        msg = event.get_message_str() or ""
        sender = event.get_sender_id() or ""
        group = event.get_group_id() or ""

        # 群聊：一律静默（隐私铁律）
        if group:
            event.should_call_llm(False)
            return

        # 私聊：仅主人
        owner = str(self.cfg.get("owner_qq", "") or "").strip()
        if not owner or sender != owner:
            event.should_call_llm(False)
            return
        if not msg.strip():
            event.should_call_llm(False)
            return

        base = str(self.cfg.get("api_base", "") or "").strip().rstrip("/")
        token = str(self.cfg.get("api_token", "") or "").strip()
        if not base:
            logger.error("[xy] api_base 未配置，无法转发小月")
            event.should_call_llm(False)
            return

        try:
            r = await self._client.post(
                f"{base}/api/chat",
                json={"message": msg.strip()},
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
            r.raise_for_status()
            reply = r.json().get("reply", "") or ""
        except Exception as e:
            logger.warning(f"[xy] 小月服务调用失败: {type(e).__name__}: {e}")
            reply = "😅 小月服务暂时不可达（服务器在重启？），稍后再试"

        reply = reply.strip()
        if len(reply) > REPLY_MAX_CHARS:
            reply = reply[:REPLY_MAX_CHARS] + "\n…（内容过长已截断，完整版去电脑面板看）"

        event.set_result(event.make_result().message(reply).use_t2i(False))
        event.should_call_llm(False)

    async def terminate(self):
        try:
            await self._client.aclose()
        except Exception:
            pass
