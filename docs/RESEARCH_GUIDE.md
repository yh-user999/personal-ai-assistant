# 研究指南 —— 给助手加"心智功能"怎么找资料

> 想加记忆/反思/人格/情绪类功能时，用什么关键词搜、去哪里找、从哪篇入门。
> 核心心法：**中文营销文少，学术术语多——学会"翻译"需求为术语，资料自然出来。**

## 一、术语地图（需求 → 学术术语 → 工程术语 → 代表作）

### 记忆
| 你想做的 | 学术/检索术语 | 工程术语 | 代表作 |
|---|---|---|---|
| 记住对话 | episodic memory（情景记忆） | conversation memory | [Generative Agents](https://arxiv.org/abs/2304.03442) |
| 提炼长期知识 | semantic memory / consolidation | fact extraction, summarization | Mem0、Zep |
| 分层记忆 | hierarchical memory / memory layers | memory tiers | [MemGPT](https://arxiv.org/abs/2310.08560) |
| 记忆检索 | memory retrieval / recency-importance-relevance | RAG over memory | Generative Agents 4.2 节 |
| 遗忘 | forgetting curve / memory decay / eviction | TTL, importance decay | 艾宾浩斯；Mem0 |

### 反思
| 你想做的 | 学术/检索术语 | 工程术语 | 代表作 |
|---|---|---|---|
| 自我反思改进 | **self-reflection / reflexion** | critique-then-refine | [Reflexion](https://arxiv.org/abs/2303.11366) |
| 定期总结心得 | reflection / introspection | weekly reflection | Generative Agents（reflection 树） |
| 从错误学习 | self-correction / error memory | lessons store | Reflexion；[Voyager](https://arxiv.org/abs/2305.16291) |

### 人格
| 你想做的 | 学术/检索术语 | 工程术语 | 代表作 |
|---|---|---|---|
| 稳定的说话风格 | **persona / personality consistency** | system prompt persona, few-shot style | 人格表达综述（[中文综述](https://tis.hrbeu.edu.cn/en/oa/darticle.aspx?type=view&id=202505031#1)） |
| 人格维度建模 | OCEAN / Big Five / MBTI | personality traits | 大五人格论文 |
| 人设不崩 | character consistency / persona drift | drift detection | Character.AI 技术分享 |

### 情绪
| 你想做的 | 学术/检索术语 | 工程术语 | 代表作 |
|---|---|---|---|
| 感知用户情绪 | **emotion recognition / sentiment analysis** | mood detection | PAD 模型（Mehrabian 1996） |
| 情绪状态机 | affective computing / emotion model | mood tracker, valence-arousal | OCC 模型；[Agents with Feelings](https://www.semanticscholar.org/paper/Agents-with-Feelings-Personality-and-Emotion-in-Ding-Zimmermann/e7414a4600e3222711260752e9f756727347fccc) |
| 共情回复 | empathetic dialogue / emotional support | empathy prompt | EMPATHETICDIALOGUES 数据集 |

### 统一框架（强烈推荐先读）
| 术语 | 说明 |
|---|---|
| **cognitive architecture（认知架构）** | 把记忆/反思/人格/情绪统一组织起来的蓝图 |
| **CoALA** | [Cognitive Architectures for Language Agents](https://arxiv.org/abs/2309.02427)——LLM agent 认知架构的标准框架（记忆/决策/行动空间） |
| BDI | 信念-欲望-意图（Wave Memory 灵魂引擎的理论骨架） |
| mind / theory of mind | 心智理论（推测用户意图） |

## 二、检索公式（背下来）

```
需求词 + 学术后缀 = 精准结果

"LLM agent" + memory/reflection/persona/emotion + survey/review   ← 找综述入门
"LLM agent" + 术语 + arxiv                                        ← 找原始论文
术语 + "dataset"/"benchmark"                                       ← 找评测标准
术语 + "github"                                                    ← 找现成实现
```

**进阶技巧**：
1. 找不到就换层：产品词（ChatGPT 记忆）→ 技术词（memory augmentation）→ 学术词（episodic memory）→ 认知科学词（consolidation），一层层抽象
2. 论文的 **Related Work（相关工作）** 是免费文献索引——一篇好论文给你 30 篇相关论文
3. 从实现反查理论：GitHub 项目的 README 底部 References 往往是精挑细选过的
4. 找"survey"（综述）永远比一篇篇读论文高效 10 倍

## 三、去哪找

| 渠道 | 用途 | 技巧 |
|---|---|---|
| [arXiv](https://arxiv.org/list/cs.CL/recent) | 原始论文（cs.CL / cs.AI） | 搜 `abs:"LLM agent memory"` |
| [Semantic Scholar](https://www.semanticscholar.org/) | 引用关系、相关论文 | 点一篇论文看 Citations 树 |
| [Papers with Code](https://paperswithcode.com/) | 论文+代码配套 | 按任务分类浏览 |
| [ACL Anthology](https://aclanthology.org/) | NLP 顶会论文 | 情绪/人格/对话系统都在这 |
| [Hugging Face Papers](https://huggingface.co/papers) | 每日论文+讨论 | 关注 agent/cognition 标签 |
| GitHub 搜 `awesome-llm-agent` | 资源合集 | 合集里必有 memory/persona 分类 |

## 四、给你的项目定制的推荐路线（从入门到深入）

1. **先读 CoALA**（认知架构总纲，1 小时）——知道记忆/反思/人格在架构里的位置
2. **Generative Agents 4.2 节**——记忆三要素+反思树（本项目记忆系统的母体）
3. **Reflexion**——反思的工程模板（本项目自省模块的强化版）
4. 人格/情绪类 → 搜 "persona consistency LLM survey" + "affective computing LLM"
5. 想做就对照本项目的 `services/` 模式（检测→存储→注入）直接移植

## 五、本项目的对照（哪些已实现、哪些待研究）

| 功能 | 本项目现状 | 深入方向 |
|---|---|---|
| 记忆 | ✅ 双层+衰减+混合检索 | MemGPT 分层、记忆图谱 |
| 反思 | ✅ 自省+周报+每日小结 | Reflexion 的自我评估循环 |
| 人格 | 🔶 画像四维+风格范例 | persona 一致性评测、OCEAN 建模 |
| 情绪 | ❌ 未做 | PAD 模型、valence-arousal 轨迹 |
| 信念 | ❌ 未做（画像部分覆盖） | 信念强化/动摇生命周期（Wave Memory BeliefEngine） |
