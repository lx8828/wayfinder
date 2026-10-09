# 智能出行助手 Agent

一个基于 Python 的智能出行助手 Agent，支持 **ReAct** 和 **Plan-Execute** 两种模式，具备**长期记忆**能力，能根据用户历史偏好提供个性化出行建议。

## 🚧 v2 重写进行中

本项目正在从 v1 手写实现重写为 **v2 Tool Agent Runtime**。

```bash
uv sync
uv run pytest                          # 179 个离线测试，无需 API Key
uv run first-agent chat "北京今天天气"   # 需要 .env 中的 DEEPSEEK_API_KEY / QWEATHER_API_KEY
```

- 设计文档：[`docs/design.md`](docs/design.md)（v0.4，含 R1–R7 实现偏离登记与逐阶段进度）
- v1 冻结快照：Git tag `v1-handwritten` 与 [`archive/handwritten_v1/ARCHIVE.md`](archive/handwritten_v1/ARCHIVE.md)

> ⚠️ 下文「功能特性 / 项目结构 / 架构设计」描述的是 **v1**；v2 主线代码在 `src/first_agent/`，本 README 将在 Phase 5 重写。

## ✨ 功能特性

- 🌤️ **天气查询**：支持国内主要城市的实况天气
- ⏰ **时间查询**：获取当前时间
- 🧠 **长期记忆**：跨会话记住用户偏好（姓名、居住地、饮食偏好等）
- 🔄 **两种模式**：
  - **ReAct**：走一步看一步，适合简单问题
  - **Plan-Execute**：先规划再执行，适合复杂任务
- 🔧 **工具扩展**：易于添加新工具
- 🏗️ **骨架分离**：通用 Agent 骨架与业务逻辑解耦

## 🚀 快速开始

### 环境要求

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) 包管理工具
- DeepSeek API Key

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd first-agent

# 安装依赖
uv sync
```

### 配置

在项目根目录创建 `.env` 文件：

```
DEEPSEEK_API_KEY=your-api-key-here
```

### 运行

```bash
uv run python -m first_agent.main
```

启动后选择模式（1=ReAct, 2=Plan-Execute），即可开始对话。

## 💡 使用示例

### ReAct 模式（走一步看一步）

```
你：北京天气好吗
AI：北京当前天气：晴，气温28°C...
你：我这里呢          ← 自动使用记忆中的居住地
AI：重庆当前天气：多云，气温25°C...
```

### Plan-Execute 模式（先规划再执行）

```
你：我明天想去看电影，天气怎么样？
AI：[规划步骤：获取时间→获取天气]
AI：明天重庆天气晴朗，适合出门看电影！
```

### 记忆能力

```
你：我叫小明，住在成都    ← 自动保存
你：我搬到杭州了          ← 自动更新
你：我不想吃辣            ← 自动保存
（下次会话自动加载这些信息）
```

## 📁 项目结构

```
src/first_agent/
├── agent.py              # 通用 Agent 骨架
├── main.py               # 业务入口（出行助手）
├── models.py             # 数据模型（TravelState, Plan）
├── state.py              # Agent 状态管理
├── state_manager.py      # 状态提取器
├── planner.py            # 任务规划器
├── executor.py           # 计划执行器
├── memory/               # 记忆模块
│   ├── database.py       # 数据库连接与建表
│   ├── extractor.py      # 记忆提取（LLM）
│   ├── writer.py         # 记忆写入（含 upsert）
│   ├── loader.py         # 记忆加载与映射
│   ├── manager.py        # 记忆 CRUD 管理
│   ├── service.py        # 记忆服务（协调 add/update/keep）
│   └── update.py         # 记忆更新决策（LLM）
└── tools/                # 工具模块
    ├── base.py           # 工具装饰器与 Schema 生成
    ├── registry.py       # 工具注册表
    ├── weather.py        # 天气查询工具
    └── time.py           # 时间查询工具
```

## 🏗️ 架构设计

### Agent 骨架与业务分离

```
main.py (业务层)
├── TravelAssistant(Agent)
│   ├── _build_system_prompt()   # 业务 Prompt
│   ├── _extract_state()         # 业务状态提取
│   └── _plan()                  # 业务规划
│
└── agent.py (骨架层)
    ├── run()                    # 主循环
    ├── run_react()              # ReAct 循环
    ├── run_plan_execute()       # Plan-Execute 循环
    ├── _execute_tool()          # 工具执行
    └── _memory_hook()           # 记忆钩子
```

### 记忆链路

```
用户输入 → extract_memory → service.remember → writer.save_memory
                                                  ↓
                                              manager.update_memory
                                                  ↓
                                              database (SQLite)
                                                  ↓
                              loader.load_memory_into_state → AgentState
```

## 🛠️ 添加新工具

在 `src/first_agent/tools/` 下创建新文件：

```python
from first_agent.tools.base import tool

@tool(description="工具描述")
def my_tool(param: str):
    """
    工具功能说明
    
    Args:
        param: 参数描述
    """
    # 实现逻辑
    return "结果"
```

工具会自动注册，无需手动配置。

## 📝 技术栈

- **Python 3.13**
- **DeepSeek API** - LLM 服务
- **SQLite** - 本地记忆存储
- **Pydantic** - 数据校验
- **uv** - 包管理
