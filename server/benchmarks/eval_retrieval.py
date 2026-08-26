"""检索评测：Hit@k / MRR + 基线（纯向量）vs 混合检索（向量+BM25 RRF）对比。

用法（服务器上，需真实 Embedding key）：
    cd server && .venv/bin/python benchmarks/eval_retrieval.py

测试集基于已入库的 REFERENCES.md（25 块），每题标注答案块的关键词。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import knowledge  # noqa: E402
from app.models.database import connect  # noqa: E402

# ── 测试集：问题 → 正确答案块应包含的关键词 ────────────────
TEST_SET = [
    {"q": "记忆随时间的衰减遵循什么规律", "expect": "艾宾浩斯"},
    {"q": "记忆整合服务每几小时运行一次", "expect": "4h 周期"},
    {"q": "互补学习系统的两层存储对应项目什么结构", "expect": "互补学习系统"},
    {"q": "有哪些开源的记忆框架可以对比", "expect": "Mem0"},
    {"q": "记忆被召回后 importance 怎么变化", "expect": "bump_importance"},
    {"q": "Generative Agents 的记忆检索三要素是什么", "expect": "recency"},
    {"q": "向量索引用什么近邻算法", "expect": "HNSW"},
    {"q": "wave memory 的摘要整合 prompt 输出什么字段", "expect": "facts"},
]


def _hit_rank(hits: list[dict], expect: str) -> int:
    """返回正确答案的排名（1 起），未命中返回 0。"""
    for i, h in enumerate(hits, 1):
        if expect in h["content"]:
            return i
    return 0


def evaluate(name: str, search_fn, top_k: int = 5) -> dict:
    """跑一遍测试集，输出指标。search_fn 需为 async (q, top_k) -> hits。"""
    hit1 = hitk = 0
    rr_sum = 0.0
    detail = []
    for case in TEST_SET:
        hits = asyncio.run(search_fn(case["q"], top_k))
        rank = _hit_rank(hits, case["expect"])
        if rank == 1:
            hit1 += 1
        if rank:
            hitk += 1
            rr_sum += 1.0 / rank
        detail.append((case["q"], rank, [round(h["similarity"], 3) for h in hits[:3]]))
    n = len(TEST_SET)
    return {
        "name": name,
        "hit_at_1": round(hit1 / n, 3),
        f"hit_at_{top_k}": round(hitk / n, 3),
        "mrr": round(rr_sum / n, 3),
        "detail": detail,
    }


# ── BM25 简化版（字符 2-gram，无外部依赖）──────────────────

def bm25_rank(query: str, top_k: int = 10) -> list[dict]:
    """关键词匹配排名：query 的字符 2-gram 在块中的出现次数加权。"""
    grams = [query[i:i + 2] for i in range(max(1, len(query) - 1))]
    conn = connect()
    try:
        rows = conn.execute("SELECT id, doc_name, chunk_index, content FROM knowledge_chunks").fetchall()
    finally:
        conn.close()
    scored = []
    for r in rows:
        score = sum(r["content"].count(g) for g in grams)
        if score > 0:
            scored.append((score, dict(r)))
    scored.sort(key=lambda x: -x[0])
    return [c for _, c in scored[:top_k]]


async def hybrid_search(query: str, top_k: int = 5) -> list[dict]:
    """RRF 融合：向量检索 + BM25 的排名取倒数融合（k=60 经典参数）。"""
    vec_hits = await knowledge.search_knowledge(query, top_k=10)
    bm25_hits = bm25_rank(query, 10)
    rrf: dict[int, float] = {}
    for rank, h in enumerate(vec_hits, 1):
        rrf[h["id"]] = rrf.get(h["id"], 0) + 1 / (60 + rank)
    for rank, h in enumerate(bm25_hits, 1):
        rrf[h["id"]] = rrf.get(h["id"], 0) + 1 / (60 + rank)
    ranked = sorted(rrf.items(), key=lambda kv: -kv[1])[:top_k]
    # 补回内容
    by_id = {h["id"]: h for h in vec_hits + bm25_hits}
    out = []
    for cid, score in ranked:
        if cid in by_id:
            h = by_id[cid]
            h["rrf"] = round(score, 4)
            out.append(h)
    return out


async def main() -> None:
    print("=" * 60)
    print("检索评测：基线（纯向量） vs 混合（向量+BM25 RRF）")
    print("=" * 60)
    base = evaluate("基线·纯向量", lambda q, k: knowledge.search_knowledge(q, top_k=k))
    hybrid = evaluate("混合·RRF", lambda q, k: hybrid_search(q, top_k=k))

    print(f"\n{'指标':<10}{base['name']:<20}{hybrid['name']}")
    for key in ("hit_at_1", "hit_at_5", "mrr"):
        print(f"{key:<10}{base[key]:<20}{hybrid[key]}")

    print("\n逐题对比（排名 | 基线相似度 | 混合 RRF）：")
    for (q, r1, s1), (_, r2, _) in zip(base["detail"], hybrid["detail"]):
        mark = "✅" if r2 and (r1 == 0 or r2 <= r1) else ("⚠️" if r2 == 0 and r1 else "  ")
        print(f"{mark} {q[:20]:<22} 基线第{r1 or '-'}名 → 混合第{r2 or '-'}名")


if __name__ == "__main__":
    asyncio.run(main())
