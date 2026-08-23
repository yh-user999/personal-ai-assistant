# Server —— 服务端（JD 服务器）

FastAPI 单进程服务，7×24 在线。承担：聊天编排、记忆闭环、行为事件接收、统计聚合、画像更新、周报生成。

## 目录结构

```
server/
├── run.py                  # 启动入口
├── app/
│   ├── main.py             # FastAPI 实例 + 路由注册 + 启动钩子
│   ├── config.py           # 配置加载（.env / 环境变量）
│   ├── api/                # HTTP 路由
│   │   ├── chat.py         #   聊天接口（记忆检索 + LLM 编排）
│   │   ├── events.py       #   行为事件接收（采集器推送）
│   │   ├── stats.py        #   行为统计查询
│   │   └── reports.py      #   周报查询/手动触发
│   ├── core/               # 核心机制
│   │   ├── llm.py          #   LLM 客户端（OpenAI 兼容，DeepSeek）
│   │   ├── embedding.py    #   Embedding 客户端
│   │   ├── memory.py       #   记忆写入/检索/注入
│   │   └── scheduler.py    #   APScheduler 定时任务（摘要整合/周报）
│   ├── services/           # 业务服务
│   │   ├── consolidation.py  # 摘要整合（LLM 结构化：summary/topics/facts）
│   │   ├── profile.py        # 四维度画像（增量更新）
│   │   ├── weekly_reflect.py # 每周学习反思
│   │   ├── worklog.py        # 手动工作日志
│   │   └── analyzer.py       # 行为统计聚合
│   ├── models/
│   │   └── database.py     # SQLite 建表 + sqlite-vec 接入
│   └── web/                # 前端页面（聊天/仪表盘）
├── tests/                  # pytest 冒烟测试
└── requirements.txt
```

## 启动

```bash
pip install -r requirements.txt
cp ../.env.example ../.env   # 填 API Key
python run.py                # http://0.0.0.0:8000
```

## 实现状态

- [x] 目录骨架
- [ ] M1 记忆闭环（聊天 + 摘要整合 + 检索注入）
- [ ] M2 行为事件接收 + 统计 API
- [ ] M3 周报服务 + Web 界面
