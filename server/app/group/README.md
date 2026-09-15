# app/group — 群聊领域模块

本包集中安放群聊（QQ 群）相关的服务端逻辑：短期上下文、心流节奏、主动插话评分、
注意力表达、关怀、群级表达学习、抽象关系与画像抽取。

## 当前可达性（重要）

- 这些模块**仅在群聊路径可达**：`app/chat/pipeline.py` 依赖 `ctx.is_group`（即请求带
  `group_id`）才会进入群聊分支。
- 当前**没有下游入口**：网页端与桌面端从不传递 `group_id`；QQ 侧 NapCat 仍在线，但
  OneBot 事件已无消费者（MaiBot/AstrBot 链路已于 2026-09-15 删除），因此群聊分支在
  线上实际不可达。
- 代码**有意保留**：等待新的 QQ 接入方案落地后复用，届时按 `docs/模块开发指南.md`
  的契约接入即可。删除或大改本包前先确认新 QQ 架构的决策。

## 模块清单

| 文件 | 原路径 | 职责 |
|---|---|---|
| `context.py` | `app/chat/group_context.py` | 群聊短期话题上下文 |
| `interjection.py` | `app/chat/group_interjection.py` | 非直达消息兴趣评分、冷却、小时上限与 reservation |
| `heartflow.py` | `app/chat/heartflow.py` | 群级活跃度、能量与连续回复限制 |
| `attention.py` | `app/chat/attention_drift.py` | 注意力漂移/表达节奏 |
| `care.py` | `app/services/group_care.py` | 群聊关怀行为 |
| `expression.py` | `app/services/group_expression.py` | 群级表达学习（不写入个人画像） |
| `relationship.py` | `app/services/group_relationship.py` | 抽象互动熟悉度 |
| `profile.py` | `app/services/group_profile_extract.py` | 群画像抽取（QQ 收录端点调用） |

`app/chat/social_replay.py` 已迁至 `benchmarks/social_replay.py`（离线评估工具，不属于运行时代码）。

## 边界规则

- 群聊请求始终使用 QQ 访客身份；不得读取或注入私聊画像、主人事实或其他群数据。
- 群级表达学习不得写入个人画像。
- 修改本包后应跑 `pytest tests/test_group_*.py tests/test_heartflow.py -q` 与
  `ruff check app` 验证。
