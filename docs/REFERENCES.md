# REFERENCES —— 参考来源与学习指南

> 本项目的记忆、分析、情感化设计均有明确参考来源。
> 本文档按「从本项目代码出发，逐层深入」组织，既是来源清单，也是学习路径。
>
> 阅读建议：先读 [实施方案细则](实施方案细则.md)，再按本文档第三节「学习路径」推进。

---

## 一、快速导航

| 层 | 主题 | 关键来源 | 对应本项目代码 |
|----|------|----------|----------------|
| L1 | 直接代码参考 | Wave Memory / VCP TagMemo | `services/consolidation.py`、`core/memory.py` |
| L2 | 记忆的认知科学基础 | 遗忘曲线、记忆巩固、互补学习系统 | `core/memory.py` 的衰减评分、双层存储 |
| L3 | LLM Agent 记忆范式 | Generative Agents、Mem0、Zep、Letta | `core/memory.py`、`services/weekly_reflect.py` |
| L4 | 检索与 RAG 工程 | HNSW、混合检索、sqlite-vec | `models/database.py`、`core/memory.py` |
| L5 | 行为分析与反思 | Reflexion、用户画像 | `services/analyzer.py`、`services/profile.py` |
| L6 | 情感化（未来扩展） | BDI、PAD 情绪模型、好感度 | （v1 未实现，蓝图在实施方案第九节） |

---

## 二、逐层详解

### L1 · 直接代码参考（本项目骨架抄了这些）

#### 1.1 Wave Memory（AstrBot 插件）

- 公开源: <https://github.com/vivy1024/astrbot_plugin_wave_memory>
- 你的私有版本: `yh-user999/astrbot_plugin_wave_memory`（本文档最初评估的版本 v1.1.3）
- 作者: vivy1024（移植与灵魂引擎工程实现）、lioensky（算法原作者）

**本项目直接借鉴的部分：**

| Wave Memory 组件 | 借鉴点 | 本项目落点 |
|---|---|---|
| `services/consolidation.py` | LLM 摘要整合 prompt：`summary / topics / facts(三元组) / relations` | `server/app/services/consolidation.py`（照抄改造，去群聊维度） |
| `engine/db/memory_repo.py` | memories 表结构、importance 字段 | `server/app/models/database.py` |
| 注入格式 | `[记忆] {sender}({time}): {content}` | `server/app/core/memory.py` 的 `INJECT_FORMAT` |
| importance 生命周期 | 被召回 +0.02（上限 3.0），检索评分 = 相似度 × importance × 时间衰减 | `core/memory.py` 的 `bump_importance` 与评分函数 |
| 淘汰策略 | noise 7 天、chat 30 天 | `services/analyzer.py` 的 `evict_stale` |

**学习时重点读（公开版仓库内）：**
- `services/consolidation.py` —— 看它如何设计 JSON 输出约束（`facts 最多5个、subject 必须含人名`）
- `main.py` 的 `inject_memory`（约 L1001 起）—— 并行注入管线（主搜索/经历/关系/BookLore/Soul 五通道，asyncio.gather + 3s 超时）。本项目简化为单通道，但你日后加"关系记忆/经历通道"时可回来抄
- `benchmarks/run_ablation.py` —— 消融实验方法（Hit@k/MRR），这是"如何科学评估记忆检索"的范本

#### 1.2 VCP TagMemo 浪潮认知引擎（算法本源）

- VCPChat: <https://github.com/lioensky/VCPChat>
- VCPToolBox: <https://github.com/lioensky/VCPToolBox>

Wave Memory 的五阶段检索管线全部源自这里：EPA 嵌入投影分析 → 残差金字塔（Gram-Schmidt 语义分解）→ 脉冲传播（共现图能量扩散）→ 向量融合 → 测地线重排。

**学习建议**：本项目 v1 **刻意没用**这套管线（个人数据量 BM25+向量足够）。但当你的记忆库超过 10 万条、需要多跳联想时再回来看。先读其文档目录的 `docs/` 架构说明，再读 `engine/` 源码。

---

### L2 · 记忆的认知科学基础（为什么这么设计）

理解这一层，你就明白"importance × 时间衰减""双层存储"不是拍脑袋，而是对认知机制的工程模拟。

| 理论 | 核心观点 | 本项目对应 |
|---|---|---|
| **艾宾浩斯遗忘曲线**（Ebbinghaus, 1885） | 记忆随时间指数衰减，复习可重置衰减 | `core/memory.py`：`decay = 0.5 ** (age_days / 30)`（30 天半衰期） |
| **记忆巩固**（consolidation；睡眠重放假说） | 海马体短期记忆在休息/睡眠时重放，转为新皮层长期结构 | `services/consolidation.py` 的 4h 周期整合（Wave Memory 叫"做梦"，6h 周期） |
| **互补学习系统 CLS**（McClelland, McNaughton & O'Reilly, 1995） | 海马体负责快速情境记忆，新皮层负责慢速结构化知识 | 本项目双层：memories（情境/向量）+ facts（语义/三元组）—— 这是 Mem0/Zep 等所有记忆框架的共同底座 |
| **多存储模型**（Atkinson & Shiffrin, 1968；Baddeley & Hitch 工作记忆, 1974） | 感觉→工作→长期三级存储 | 对话上下文（工作记忆/短期）→ 摘要整合（巩固）→ facts/画像（长期） |
| **间隔重复**（spaced repetition，SuperMemo 算法） | 按遗忘曲线安排复习时机最高效 | 画像每周刷新 + importance 提升 = 对重要记忆的"复习" |

**延伸阅读**：`Why there are complementary learning systems in the hippocampus and neocortex`（McClelland et al., 1995）是必读论文，理解它 = 理解整个记忆层的设计哲学。

---

### L3 · LLM Agent 记忆范式（工程实现参考）

#### 3.1 Generative Agents —— 最重要的参考（斯坦福小镇）

- 论文: <https://arxiv.org/abs/2304.03442>

**它定义了 Agent 记忆三件套，本项目全部对应：**

| Generative Agents 概念 | 含义 | 本项目对应 |
|---|---|---|
| **memory stream**（记忆流） | 所有经历以自然语言条目持续记录 | `memories` 表 |
| **retrieval 三要素**：recency / importance / relevance | 检索评分 = 时间近 × 重要性 × 相关性 | `core/memory.py` 评分函数（相似度 × importance × 时间衰减） |
| **reflection**（反思） | 定期把记忆流抽象为更高层结论 | `services/weekly_reflect.py`（每周反思）+ `services/consolidation.py`（摘要整合） |

**学习建议**：读论文第 4 节（Agent Architecture），重点看 Figure 2 的检索评分公式和 reflection 树。这是本项目"分析/反思"设计的第一源头。

#### 3.2 记忆框架三件套（选型参考）

| 框架 | 核心思路 | 何时值得再看 |
|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | 记忆提取/去重/更新（ADD 原则），带图记忆 | 想要"记忆自动纠错、冲突消解"时 |
| [Zep](https://github.com/getzep/zep) | 时序知识图谱记忆，记录实体随时间的关系 | 想要"实体关系时间线"时（对应 Wave Memory 的知识图谱） |
| [Letta（MemGPT）](https://github.com/letta-ai/letta) | 自编辑记忆 + 记忆分层（in-context / external storage） | 想要"agent 自己决定记什么、忘什么"时 |

- 有人整理过对比: [memory_system_comparison.md](https://github.com/zycaskevin/Vault-Agent-Memory/blob/main/docs/memory_system_comparison.md)

**学习建议**：先通读对比文档建立全局观；本项目 v1 的选择是"自研轻量版"（SQLite + 向量），理由是个人数据量下框架服务（Redis/Zep 需 Docker）是过度设计。等需求变重再迁移。

---

### L4 · 检索与 RAG 工程

| 主题 | 参考 | 本项目落点 |
|---|---|---|
| 向量索引 | HNSW 论文（Malkov & Yashunin, 2016）；hnswlib | v1 用 sqlite-vec 暴力检索（个人数据量足够），量级上来换 hnswlib（Wave Memory 的做法） |
| sqlite-vec | <https://github.com/asg017/sqlite-vec> | `models/database.py` 的 `memory_vectors` 虚拟表 |
| 混合检索 | BM25（关键词）+ 向量（语义）互补 | `core/memory.py`：向量失败/无结果时退化为 LIKE 关键词 |
| RAG 范式 | LangChain / LlamaIndex 的 retriever → 注入 → 生成 | `api/chat.py` 的检索注入流程 |

**学习建议**：本项目检索目前是最简实现，深入方向 = ① 混合检索权重调优（RRF 融合算法）② 加入 Wave Memory 的残差金字塔做多义消解。可先看 `benchmarks/` 的评测方法再动手。

---

### L5 · 行为分析与反思

| 来源 | 内容 | 本项目对应 |
|---|---|---|
| **Reflexion**（Shinn et al., 2023, <https://arxiv.org/abs/2303.11366>） | agent 失败后自我反思生成经验，存进记忆供下次使用 | 周报"成长点"、自省设计（v1 简化） |
| **Generative Agents reflection** | 反思树：低层观察 → 高层结论 | `weekly_reflect.py` 的反思模板 |
| 用户画像/persona（推荐系统用户建模） | 用结构化维度描述用户偏好，带置信度 | `profile.py` 四维度 + confidence |
| 统计与生成分离原则 | 确定性计算（SQL 聚合）与生成（LLM 解读）解耦 | `analyzer.py`（纯 SQL）→ 周报 LLM |

**设计原则**：行为分析里的"事实"（几点工作、提交几次）永远用代码算，LLM 只负责"解读"——防止模型幻觉污染数据。这是本项目最重要的工程原则之一。

---

### L6 · 情感化设计（v1 未实现，未来扩展参考）

> 方案评审时灵魂引擎被砍（见实施方案细则第九节）。若日后要加"温度"，按此清单回来。

| 设计 | 参考来源 | 说明 |
|---|---|---|
| BDI 心智架构 | Bratman 1987（哲学源头）；Rao & Georgeff 1995（计算化） | 信念 Belief / 欲望 Desire / 意图 Intention；Wave Memory 的信念引擎、欲望引擎、关切追踪都源于此 |
| 情绪维度模型 | Russell 环形模型（circumplex, 1980）；PAD 三维模型（Mehrabian, 1996） | Wave Memory `MoodTrajectory` 就是 valence/arousal 二维情绪轨迹 |
| 情绪与认知耦合 | [Max 认知架构](http://www.becker-asano.de/ADS04_Springer_LNCS_SimulatingEmotionDynamicsMax.pdf)（Becker-Asano） | 情绪如何影响决策与表达 |
| 好感度/亲密度 | 社交计算信任模型；指数衰减思想（同 Hacker News 热度） | Wave Memory 多维好感度（familiarity/trust/fun/depth/hostility + 半衰期 + 每日上限） |
| 人格 | OCEAN 五大人格 | Wave Memory 人格进化（好感度 → 四级态度 → 动态 prompt） |

**学习建议**：先读 Wave Memory 的 `services/mood_trajectory.py` 与 `persona_evolution.py`（代码最短、最易读），再读 Russell 环形模型的论文建立理论框架。

---

## 三、学习路径（建议顺序）

### 阶段一：读懂本项目（1-2 天）
1. 读 `docs/实施方案细则.md` —— 整体架构与决策
2. 跟读 `server/app/api/chat.py` → `core/memory.py` → `services/consolidation.py`（一条完整消息的生命周期）
3. 跑通冒烟测试：`cd server && pip install -r requirements.txt && pytest tests/ -v`

### 阶段二：理解记忆为什么这么设计（2-3 天）
1. 读 Wave Memory `services/consolidation.py` 的完整 prompt 约束
2. 读 CLS 论文（McClelland 1995）核心章节
3. 读 Generative Agents 论文第 4 节（检索三要素 + reflection）
4. 对照本项目代码标注"每一行设计对应的理论"

### 阶段三：深入检索与评测（3-5 天）
1. 读 Wave Memory `engine/query_engine.py` 与 `benchmarks/run_ablation.py`
2. 用本项目数据跑一次消融（加/减 importance、加/减向量通道）
3. 读 sqlite-vec 与 HNSW 文档，评估本项目何时需要升级

### 阶段四：扩展实践（按需）
- 想要"温度" → 移植 Wave Memory 情绪轨迹（L6）
- 想要"自动纠错记忆" → 研究 Mem0 的 ADD 管线（L3）
- 想要"知识图谱" → 研究 Zep / Wave Memory WebUI（L3）

---

## 四、深入学习的配套资源

| 资源 | 用途 |
|---|---|
| <https://arxiv.org/abs/2304.03442> | Generative Agents 原文（记忆/反思范式源头） |
| <https://arxiv.org/abs/2310.08560> | MemGPT 论文（分层记忆） |
| <https://arxiv.org/abs/2303.11366> | Reflexion 论文（自我反思） |
| <https://arxiv.org/abs/1603.09320> | HNSW 论文（近似最近邻） |
| 智谱 BigModel / DeepSeek 开放平台文档 | Embedding 与 LLM 接口细节 |
| sqlite-vec 官方文档 | 向量表 SQL 语法 |

## 五、许可证提醒

- Wave Memory / VCP 系列为 **AGPLv3**：个人自用无限制；若日后将借鉴代码发布为闭源商业项目需注意合规（本文档建议"抄思路不抄代码"）
- 本项目 `README.md` 标注为私有项目
