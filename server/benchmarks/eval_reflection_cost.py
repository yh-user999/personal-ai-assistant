"""审校成本评测：触发率（零成本）与审校延迟分布（真实调用）。

## 为什么需要

审校是同步阻塞在回复之前的：它每慢一秒，用户就多等一秒。定预算值
（REFLECTION_REVIEW_BUDGET）不能靠感觉——过小会让质量门槛时灵时不灵，
过大则失去"封顶"的意义。需要先拿到两个量：

1. **触发率**：哪些消息真的会触发审校（纯规则计算，零 LLM 成本）；
2. **延迟分布**：触发时审校实际耗时多少（必须真实调用，离线估算不可信）。

## 用法

    .venv/bin/python benchmarks/eval_reflection_cost.py --dry-run     # 只算触发率，不调 LLM
    .venv/bin/python benchmarks/eval_reflection_cost.py -n 12         # 真实测 12 次审校延迟

注意：真实模式会消耗 token（每次约 1.5k 输入），但**不写数据库**——
只调审校、不落 reply_reviews，也不碰 memories。
"""
import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chat import review as review_mod  # noqa: E402
from app.chat.context import ChatContext, ChatRequest  # noqa: E402
from app.config import settings  # noqa: E402
from app.core import llm  # noqa: E402

# 代表性消息：覆盖寒暄、事实、检索、分析、情绪、道德、创作
MESSAGES = [
    ("寒暄", "你好"),
    ("确认", "收到，谢谢"),
    ("时间", "今天周几"),
    ("事实", "李羽的能力是什么"),
    ("事实", "我之前说的小说主线是什么"),
    ("检索", "最近有什么科技新闻"),
    ("检索", "帮我查查最近有没有关于具身智能的报道"),
    ("分析", "这个方案有什么问题"),
    ("分析", "为什么检索召回率上不去"),
    ("情绪", "我今天有点累，什么都不想做"),
    ("道德", "这件事该不该追究，你怎么看"),
    ("道德", "网上都在骂他，你觉得对不对"),
    ("创作", "继续写第三章"),
    ("闲聊", "哈哈"),
]

# 三种回复长度：审校触发与草稿长度有关，用它们给出触发率的区间
DRAFTS = {
    "短(40字)": "好，我看一下。" * 5,
    "中(180字)": "这件事可以从几个角度看，先说要紧的。" * 10,
    "长(560字)": "先说结论，再说依据。" * 40,
}


def make_ctx(message: str) -> ChatContext:
    return ChatContext(
        request=SimpleNamespace(state=SimpleNamespace()),
        request_model=ChatRequest(message=message),
        message=message,
        uid="owner",
        is_owner=True,
    )


def make_bundle() -> SimpleNamespace:
    return SimpleNamespace(
        facts="- 项目 知识库 已入库两本小说",
        lessons="- 回复别用 emoji",
        mood="", mood_state="", behavior="", self_state="",
        knowledge_text="", mems=[],
    )


def measure_trigger_rate() -> dict[str, list[str]]:
    """触发率：纯规则，零成本。返回 {消息: 触发原因}。"""
    hits: dict[str, list[str]] = {}
    for label, message in MESSAGES:
        ctx = make_ctx(message)
        triggered = []
        for name, draft in DRAFTS.items():
            reasons = review_mod.should_reflect(ctx, make_bundle(), draft)
            if reasons:
                triggered.append(f"{name}:{'/'.join(reasons)}")
        hits[f"[{label}] {message}"] = triggered
    return hits


async def measure_latency(samples: int) -> list[int]:
    """真实调用审校，测延迟。只读不写库。"""
    runtime = SimpleNamespace(settings=settings, llm=llm)
    # 用长草稿测延迟：接近真实触发场景（短回复基本不触发），且能覆盖全部消息类型
    draft = DRAFTS["长(560字)"]
    triggered = [
        (label, message) for label, message in MESSAGES
        if review_mod.should_reflect(make_ctx(message), make_bundle(), draft)
    ]
    if not triggered:
        return []
    picks = (triggered * ((samples // len(triggered)) + 1))[:samples]
    latencies: list[int] = []
    for index, (label, message) in enumerate(picks, 1):
        started = time.monotonic()
        checked, elapsed = await review_mod.review_reply(
            make_ctx(message), runtime, make_bundle(), draft
        )
        wall = int((time.monotonic() - started) * 1000)
        latencies.append(elapsed)
        print(
            f"  {index:>2}/{len(picks)}  [{label}] {message[:14]:<16}"
            f" status={checked.status:<8} review={elapsed:>6}ms wall={wall:>6}ms"
        )
    return latencies


def report(latencies: list[int], budget: float) -> None:
    if not latencies:
        print("\n无有效样本，跳过延迟统计")
        return
    ordered = sorted(latencies)
    median = statistics.median(ordered)
    p90 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    over = [value for value in ordered if value > budget * 1000]
    print("\n=== 审校延迟分布 ===")
    print(f"  样本数   {len(ordered)}")
    print(f"  最小     {ordered[0]}ms")
    print(f"  中位数   {int(median)}ms")
    print(f"  最大     {ordered[-1]}ms")
    print(f"  P90      {p90}ms")
    print(f"  超出当前预算({budget}s)  {len(over)}/{len(ordered)}")
    print(f"\n  折算：每次触发让用户多等约 {int(median) / 1000:.1f} 秒（中位数）")


def main() -> None:
    parser = argparse.ArgumentParser(description="审校成本评测")
    parser.add_argument("-n", "--samples", type=int, default=10, help="真实审校调用次数")
    parser.add_argument("--dry-run", action="store_true", help="只算触发率，不调 LLM")
    args = parser.parse_args()

    print("=== 触发率（零成本）===")
    hits = measure_trigger_rate()
    triggered = [key for key, value in hits.items() if value]
    for key, value in hits.items():
        mark = "触发" if value else "跳过"
        detail = f"  {value}" if value else ""
        print(f"  [{mark}] {key}{detail}")
    print(f"\n  触发 {len(triggered)}/{len(hits)}（按中长回复计）")

    if args.dry_run:
        print("\n--dry-run：跳过真实延迟测量")
        return

    print(f"\n=== 真实审校延迟（{args.samples} 次，会消耗 token）===")
    latencies = asyncio.run(measure_latency(args.samples))
    report(latencies, float(getattr(settings, "reflection_review_budget", 14.0)))


if __name__ == "__main__":
    main()
