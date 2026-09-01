"""对话回归集：固定问题集 → 关键词判据 → 通过/退化表格。

TESTING_GUIDE 第三节挂了很久的"建议实现"。动因是行为层测试一直靠手动
问七八遍，改 A 坏 B 发现不了。

判据说明（重要）：这里用的是**关键词命中**，不是语义评分。
优点是零成本、确定性、可复现；代价是判据宽松——它能抓住"完全答错/失忆/
幻觉"这类硬退化，抓不住"语气变差"这类软退化。软退化仍需人眼看 reply 全文，
所以报告默认打印回复原文。

题目分两类：
- 依赖真实数据（DEPENDS_ON_REAL_DB=True）：问项目/进度/小说设定等，
  必须跑在真实库上才有意义；--dry-run 时自动跳过并标 SKIP
- 不依赖（False）：身份、时间、命令解析、防幻觉，任何库都能测

用法：
    .venv/bin/python benchmarks/chat_regression.py              # 真实库（会留对话痕迹）
    .venv/bin/python benchmarks/chat_regression.py --dry-run    # 临时库，不污染
    .venv/bin/python benchmarks/chat_regression.py -k 名字      # 只跑标题含"名字"的题
"""
import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── 定价（USD / 1M tok），用于报告末尾折算成本 ────────────────
# 来源：opencode.ai/docs/zen（deepseek-v4-flash，分峰谷）。
# 换了模型/渠道就改这里；只影响报告里的成本估算，不影响判定。
PRICE = {
    "off_peak": {"input": 0.22, "output": 0.66, "cached": 0.007},
    "peak": {"input": 0.44, "output": 1.32, "cached": 0.014},
}
USD_CNY = 7.1
# 高峰时段（UTC）：01-04 与 06-10，即北京时间 09-12 与 14-18
PEAK_UTC_RANGES = ((1, 4), (6, 10))


# ── 测试集 ────────────────────────────────────────────────
# (标题, 发给她的话, 期望命中的关键词组, 是否依赖真实库)
# 关键词组语义：外层 list 全部满足（AND），内层 tuple 任一命中即可（OR）
CHAT_REGRESSION: list[tuple[str, str, list[tuple[str, ...]], bool]] = [
    # —— 身份锚定：本轮 lessons 去重修复的核心保护对象 ——
    ("身份·名字", "你叫什么名字？", [("小月",)], False),
    # —— 真实记忆召回 ——
    ("记忆·当前项目", "我最近在做哪个项目？",
     [("AI", "LLM", "助手", "项目")], True),
    ("记忆·小说设定", "李羽的能力设定是什么？",
     [("杀人", "变强")], True),
    # —— 防幻觉：知识库里没有的东西要老实说 ——
    # 判据用"不太确定/没收录"这类短片段，别写整词——她的真实措辞是
    # "我不太确定"，写死"不确定"反而匹配不到（首版就栽在这）
    ("防幻觉·无资料", "知识库里有 Rust 所有权机制的资料吗？",
     [("没有", "确定", "没找到", "查不到", "暂无", "不记得", "没收录")], False),
    # —— 命令解析（零 LLM 路径，顺带验证没被 prompt 改动影响）——
    ("命令·工作日志", "记录：下午2-4点调参", [("已记录",)], False),
    # 时间格式是 "下午 2:20 啦（9月1日 星期二）"——认冒号或"月"，不认"点"
    ("命令·时间直答", "现在几点了", [(":", "月")], False),
    ("命令·提醒列表", "我的提醒", [("提醒", "没有")], False),
    # —— 纠正与遵守 ——
    ("自省·接受纠正", "以后回答简短一点",
     [("好", "知道", "记住", "明白", "行")], False),
    # —— 本轮新增：不确定就直说（软判据，宽松命中）——
    ("拟人·承认不确定", "我大学室友叫什么名字？",
     [("不知道", "不确定", "没提过", "不记得", "没有印象", "你没说")], False),
    # —— 格式规范：日常对话不该出现 Markdown 标记 ——
    ("格式·无 Markdown", "简单说说你能帮我做什么", [("",)], False),
]


def _is_peak() -> bool:
    from datetime import datetime, timezone

    h = datetime.now(timezone.utc).hour
    return any(lo <= h < hi for lo, hi in PEAK_UTC_RANGES)


def _cost(usage: dict) -> tuple[float, float]:
    """(USD, CNY)。cached 部分按缓存价，其余按输入价。"""
    p = PRICE["peak" if _is_peak() else "off_peak"]
    cached = min(usage.get("cached", 0), usage.get("prompt", 0))
    fresh = usage.get("prompt", 0) - cached
    usd = (
        fresh * p["input"] + cached * p["cached"] + usage.get("completion", 0) * p["output"]
    ) / 1e6
    return usd, usd * USD_CNY


def _judge(reply: str, expects: list[tuple[str, ...]], title: str) -> tuple[bool, str]:
    """关键词判定。返回 (通过, 未命中说明)。"""
    # 格式题特判：验证"没有 Markdown 标记"而不是"含某关键词"
    if title.startswith("格式·"):
        bad = [m for m in ("**", "##", "- ", "* ") if m in reply]
        return (not bad), (f"出现 Markdown 标记 {bad}" if bad else "")
    missing = []
    for group in expects:
        if not any(kw in reply for kw in group if kw):
            missing.append("/".join(k for k in group if k))
    return (not missing), ("未命中 " + "; ".join(missing) if missing else "")


async def _ask(message: str) -> str:
    """走真实聊天主路径（含全部注入与记忆写入），拿回复文本。"""
    from app.api.chat import ChatRequest, chat

    class _FakeState:
        collector_heartbeat = None

    class _FakeApp:
        state = _FakeState()

    class _FakeRequest:
        app = _FakeApp()

    resp = await chat(ChatRequest(message=message), _FakeRequest())
    return resp.reply


async def run(keyword: str | None, dry_run: bool, show_reply: bool) -> int:
    from app.core import llm

    cases = [c for c in CHAT_REGRESSION if not keyword or keyword in c[0]]
    if not cases:
        print(f"没有匹配 '{keyword}' 的题目")
        return 0

    llm.reset_usage()
    results = []
    for title, msg, expects, needs_real in cases:
        if dry_run and needs_real:
            results.append((title, "SKIP", "依赖真实库，--dry-run 下跳过", "", 0.0))
            continue
        t0 = time.monotonic()
        try:
            reply = await _ask(msg)
        except Exception as e:
            results.append((title, "ERROR", f"{type(e).__name__}: {e}", "", time.monotonic() - t0))
            continue
        ok, why = _judge(reply, expects, title)
        results.append((title, "PASS" if ok else "FAIL", why, reply, time.monotonic() - t0))

    # ── 报告 ──
    print("\n" + "=" * 74)
    print(f"对话回归集  |  {'临时库（dry-run）' if dry_run else '真实库'}  |  {len(results)} 题")
    print("=" * 74)
    icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ ", "ERROR": "💥"}
    for title, status, why, reply, secs in results:
        print(f"{icon[status]} {title:<18s} {secs:5.1f}s  {why}")
        if show_reply and reply:
            first = reply.replace("\n", " ")[:100]
            print(f"     └ {first}{'…' if len(reply) > 100 else ''}")

    n_pass = sum(1 for r in results if r[1] == "PASS")
    n_fail = sum(1 for r in results if r[1] in ("FAIL", "ERROR"))
    n_skip = sum(1 for r in results if r[1] == "SKIP")
    print("-" * 74)
    print(f"通过 {n_pass} · 退化 {n_fail} · 跳过 {n_skip}")

    u = llm.get_usage()
    if u["calls"]:
        usd, cny = _cost(u)
        cache_note = (
            f"，缓存命中 {u['cached']:,}（{u['cached'] / max(u['prompt'], 1) * 100:.0f}%）"
            if u["cached"] else "（中转未透传缓存明细）"
        )
        print(f"LLM 调用 {u['calls']} 次：输入 {u['prompt']:,} tok / 输出 "
              f"{u['completion']:,} tok{cache_note}")
        print(f"本次成本 ≈ ${usd:.4f} / {cny:.3f} 元"
              f"（{'高峰' if _is_peak() else '非高峰'}价）")
    return 1 if n_fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="对话回归集")
    ap.add_argument("--dry-run", action="store_true",
                    help="跑在临时库上，不污染真实对话记录（依赖真实数据的题会跳过）")
    ap.add_argument("-k", "--keyword", help="只跑标题含该关键词的题")
    ap.add_argument("-q", "--quiet", action="store_true", help="不打印回复原文")
    args = ap.parse_args()

    if args.dry_run:
        from app.config import settings
        from app.models.database import init_db, reset_connections

        settings.db_path = str(Path(tempfile.mkdtemp(prefix="chat-regress-")) / "t.db")
        reset_connections()
        init_db()
        print(f"[dry-run] 临时库：{settings.db_path}")

    return asyncio.run(run(args.keyword, args.dry_run, not args.quiet))


if __name__ == "__main__":
    raise SystemExit(main())
