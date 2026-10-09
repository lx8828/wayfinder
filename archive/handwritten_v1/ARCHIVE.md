# archive / handwritten_v1

v1 手写实现的**冻结快照**，仅作历史参考、机制复现和学习使用。

- 对应 Git tag：`v1-handwritten`（commit `fd2dafa`）
- **不参与 v2 主线**：不被 `src/first_agent`（v2）导入，不参与 v2 运行链路
- 设计文档：见仓库根目录 `../../design.md`（v0.3，第 1.1 节归档策略）

## 为什么单独放一份

v1 的包名与 v2 同为 `first_agent`。若两者安装进**同一个 venv**，同名包会互相覆盖，导致 import 结果不确定。因此本目录必须使用**独立环境**运行。

## 运行方式（独立环境）

```bash
cd archive/handwritten_v1
uv sync          # 在本目录创建独立的 .venv，与主线互不影响
```

首次运行前需要初始化数据库（本快照**不包含** `memory.db`，避免携带历史个人数据）：

```bash
uv run python -c "from first_agent.memory.database import init_db; init_db()"
```

然后启动：

```bash
# 命令行版
uv run python -m first_agent.main

# Web 版（局域网）
uv run python web.py
# Web 版（ngrok 公网穿透）
uv run python web.py --ngrok
```

## 环境变量

需要在**本目录**创建 `.env`（已被 `.gitignore` 排除，不会进版本库）：

```
DEEPSEEK_API_KEY=...
QWEATHER_HOST=...
QWEATHER_API_KEY=...
NGROK_AUTHTOKEN=...     # 仅 --ngrok 时需要
```

仓库中**不含任何密钥**；请勿把 `.env` 提交或复制回主线目录。

## 与主线的差异

| 项 | v1（本目录） | v2（主线 `src/`） |
|---|---|---|
| ReAct | 原生 `tool_calls` 裸循环，无 Thought 归一化 | ReAct-style（Thought 可选），见 design.md 7 节 |
| Plan | `steps` 仅工具名，无参数/依赖 | `PlanStep` + `depends_on` + `$steps.<id>.<field>` |
| 状态 | 每轮重置 `AgentState` | `SessionStore` 持久化 `WorkingState` |
| 记忆 | `add/update/keep` 由 LLM 判定，`add` 可能丢数据 | LLM 判关系，代码裁决动作 |
| 入口 | `pyproject.toml` 的 `first-agent` 指向占位函数 | 指向真实 CLI |

> 注意：v1 的 `pyproject.toml` 里 `first-agent = "first_agent:main"` 指向的是占位函数（只打印一行问候），**真实入口是 `python -m first_agent.main` 或 `web.py`**。保留原样以维持快照保真度。
