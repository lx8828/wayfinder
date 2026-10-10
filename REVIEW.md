# 📚 智能出行助手 Agent - 复习文档

> 本文档用于复习整个项目的学习内容。每节都有**代码位置**和**原理解释**，忘记时直接对照代码查阅。

---

## 🗺️ 学习路径总览

```
Lv0 调 API ──→ Lv1 ReAct/Plan-Execute ──→ Lv2 记忆系统 ──→ Lv3 骨架分离
   │                │                        │                    │
   │                │                        │                    └─ Agent 基类
   │                │                        │                       业务子类
   │                │                        │
   │                │                        └─ 提取→写入→加载→映射
   │                │                           CRUD + upsert + category
   │                │
   │                └─ 工具调用循环
   │                   状态管理
   │
   └─ prompt 工程
      JSON 结构化输出
```

---

## 第一部分：Agent 核心概念

### 1.1 什么是 Agent

Agent = **LLM + 工具 + 循环控制**。LLM 负责"想"，工具负责"做"，循环负责"驱动"。

```
用户输入 → LLM 思考 → 调用工具 → 拿到结果 → LLM 再思考 → ... → 最终回答
```

### 1.2 两种 Agent 模式

#### ReAct（Reason + Act）

**核心思想**：走一步看一步，LLM 自主决定何时调用工具、调用哪个。

**代码位置**：`agent.py` 的 `run_react()` 方法

**执行流程**：
```
1. 把用户输入加到 messages
2. 循环：
   a. 调 LLM（带 tools 参数）
   b. LLM 返回：要么是最终回答，要么是工具调用
   c. 如果是工具调用：执行工具，把结果加回 messages，继续循环
   d. 如果是最终回答：结束循环
3. 打印答案，保存到 messages
```

**关键代码**：
```python
# agent.py 第 60-64 行
response = self.client.chat.completions.create(
    model="deepseek-chat",
    messages=self.messages,
    tools=self.tools,           # 告诉 LLM 有哪些工具可用
    tool_choice="auto"          # LLM 自主决定是否调用
)
```

**特点**：灵活但不可控，LLM 可能反复调用工具。

#### Plan-Execute（先规划再执行）

**核心思想**：先让 LLM 规划好所有步骤，再逐步执行，最后让 LLM 总结。

**代码位置**：`agent.py` 的 `run_plan_execute()` 方法

**执行流程**：
```
1. 重置 state，加载记忆
2. 从用户输入提取状态（destination/time/purpose）
3. 调 LLM 生成计划：需要哪些步骤、是否缺少信息
4. 如果缺信息 → 问用户
5. 如果有步骤 → 逐步执行工具
6. 把执行结果给 LLM，生成最终回答
```

**关键代码**：
```python
# planner.py 第 5-93 行
def create_plan(client, state, tools):
    # 把状态和工具列表给 LLM
    # LLM 返回：{"need_info": [], "steps": ["get_current_time", "get_weather"]}
    return Plan.model_validate_json(...)

# executor.py 第 4-24 行
def execute_plan(plan, state, tool_registry):
    # 按步骤顺序执行，从 state 中取参数
    for step in plan.steps:
        func = tool_registry.get(step)
        kwargs = {name: state.get(name) for name in 函数参数 if state 里有}
        results[step] = func(**kwargs)
```

**特点**：可控、可预测，但不够灵活。

### 1.3 两种模式对比

| | ReAct | Plan-Execute |
|--|-------|-------------|
| 决策方式 | LLM 自主逐步决策 | 先规划后执行 |
| 优点 | 灵活，适应复杂场景 | 可控，可预测 |
| 缺点 | 不可控，可能反复调用 | 不够灵活 |
| 适用场景 | 多轮对话、不确定的请求 | 固定流程、需要可预测 |

---

## 第二部分：工具系统

### 2.1 什么是工具

工具 = **LLM 可调用的外部函数**。LLM 本身没有实时信息、没有执行能力，需要通过工具获取。

### 2.2 工具装饰器

**代码位置**：`tools/base.py` 的 `@tool()` 装饰器

**作用**：
1. 给函数添加 `tool_description` 属性
2. 把函数注册到全局 `tools` 列表
3. 自动生成 JSON Schema（给 LLM 看的工具描述）

**工作原理**：
```python
# base.py 第 33-43 行
def tool(description=""):
    def decorator(func):
        func.tool_description = description  # 存描述
        tools.append(func)                    # 注册到列表
        return func
    return decorator
```

### 2.3 工具注册与 Schema 生成

**代码位置**：`tools/registry.py` 和 `tools/base.py` 的 `get_tool_schemas()`

**流程**：
```
1. 导入 weather.py, time.py → @tool 自动注册到 tools 列表
2. registry.py 遍历 tools 列表，生成 name→func 的映射
3. get_tool_schemas() 遍历 tools 列表，为每个函数生成 JSON Schema
```

**JSON Schema 长这样**（给 LLM 看的）：
```json
{
  "type": "function",
  "function": {
    "name": "get_weather",
    "description": "查询指定城市天气",
    "parameters": {
      "type": "object",
      "properties": {
        "destination": {
          "type": "string",
          "description": "城市名称"
        }
      },
      "required": ["destination"]
    }
  }
}
```

### 2.4 工具执行

**代码位置**：`agent.py` 的 `_execute_tool()` 方法

```python
# agent.py 第 167-174 行
def _execute_tool(self, tool_call):
    name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)  # LLM 传的参数是 JSON 字符串
    tool = self.tool_registry.get(name)               # 从注册表找函数
    return tool(**args)                               # 调函数
```

### 2.5 添加新工具的步骤

1. 在 `tools/` 下创建文件
2. 用 `@tool(description="...")` 装饰函数
3. 写 Google 风格 docstring（`Args:` 部分用于生成参数描述）
4. 在 `base.py` 的 `get_tool_schemas()` 里导入新模块

---

## 第三部分：状态管理

### 3.1 什么是状态

状态 = **Agent 当前所知的上下文信息**。在出行场景下，包括目的地、时间、出行目的。

### 3.2 AgentState 类

**代码位置**：`state.py`

```python
class AgentState:
    def __init__(self):
        self.data = {}
    
    def set(self, key, value):
        self.data[key] = value
    
    def get(self, key):
        return self.data.get(key)
    
    def show(self):
        return self.data
```

**为什么用这么简单的类？**
- 状态数据量小（几个 key-value）
- 需要在不同模块间传递
- 后期可以扩展（加类型校验、来源标记等）

### 3.3 状态提取

**代码位置**：`state_manager.py` 的 `extract_state()`

**作用**：从用户输入中提取结构化的状态信息。

```python
# state_manager.py 第 3-37 行
def extract_state(client, user_input):
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": "提取 destination/time/purpose"},
            {"role": "user", "content": user_input}
        ],
        response_format={"type": "json_object"}  # 强制返回 JSON
    )
    return TravelState.model_validate_json(data)  # 用 Pydantic 校验
```

**关键点**：
- `response_format` 强制 LLM 返回 JSON，避免格式错误
- `Pydantic` 校验数据，`None` 表示未提取到

### 3.4 状态与记忆的合并

**代码位置**：`agent.py` 的 `run_plan_execute()` 方法

**合并策略**：
```
1. 每轮对话重置 state（避免上一轮残留）
2. 从记忆加载默认值（如居住地=重庆）
3. 用提取结果覆盖（用户说"北京"→ destination=北京）
4. 如果提取结果为 None，保留记忆默认值
```

**这就是 Bug 2 的修复原理**：每轮重置，记忆作为默认值，用户明确说过的覆盖记忆。

---

## 第四部分：记忆系统（重点）

### 4.1 为什么需要记忆

没有记忆的 Agent 每次对话都是"失忆"状态。记忆让 Agent 能：
- 记住用户的姓名、偏好
- 跨会话保持上下文
- 提供个性化服务

### 4.2 记忆系统架构

```
┌─────────────────────────────────────────────────┐
│                  记忆系统全链路                   │
│                                                   │
│  用户输入                                         │
│     │                                             │
│     ▼                                             │
│  extract_memory()  ─── LLM 提取记忆               │
│     │                                             │
│     ▼                                             │
│  remember()        ─── 协调 add/update/keep       │
│     │                                             │
│     ├── save_memory()   ─── 新记忆写入            │
│     │                                             │
│     └── update_memory() ─── 旧记忆更新            │
│         │                                         │
│         ▼                                         │
│     SQLite 数据库                                 │
│         │                                         │
│         ▼                                         │
│  load_memory_into_state() ─── 加载到 state        │
│     │                                             │
│     ▼                                             │
│  KEY_MAP 映射 ─── 把"居住地"映射为"destination"   │
│     │                                             │
│     ▼                                             │
│  AgentState ─── 供 executor 取参数               │
└─────────────────────────────────────────────────┘
```

### 4.3 数据库设计

**代码位置**：`memory/database.py`

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,       -- 记忆键（如"居住地"）
    value TEXT NOT NULL,            -- 记忆值（如"重庆"）
    category TEXT,                  -- 分类（如"用户身份"）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
```

**设计要点**：
- `key` 唯一约束：同一键只有一条记录
- `category` 分类：便于扩展（如用户身份/偏好/习惯）
- `UNIQUE` + `ON CONFLICT DO UPDATE`：实现 upsert

### 4.4 记忆提取

**代码位置**：`memory/extractor.py` 的 `extract_memory()`

**Prompt 设计要点**：
1. 明确什么该保存、什么不该保存
2. 强制每条信息单独提取（不合并）
3. 规定标准 key 名称（便于 KEY_MAP 映射）
4. 要求返回 JSON 格式

```python
# extract_memory 的 system prompt
"""
值得保存：用户身份、偏好、习惯、长期计划
不要保存：一次性事件、临时天气、普通聊天
key 标准名称：用户姓名、居住地、饮食偏好...
"""
```

**返回格式**：
```json
{"memories": [{"key": "居住地", "value": "重庆", "category": "用户身份"}]}
```

### 4.5 记忆服务

**代码位置**：`memory/service.py` 的 `remember()`

**决策流程**：
```
1. 检查记忆是否有效（有 key 才处理）
2. 查数据库是否已有这条记忆
   ├── 没有旧记忆 → save_memory() → 返回 {"action": "add"}
   └── 有旧记忆 → 交给 LLM 判断
       ├── LLM 返回 update → update_memory() → 返回 {"action": "update"}
       ├── LLM 返回 keep → 什么都不做 → 返回 {"action": "keep"}
       └── LLM 返回 add → ⚠️ 未处理（已知问题）
```

**关键代码**：
```python
# service.py 第 23-63 行
old = get_memory(key)

if not old:
    save_memory(memory)
    return {"action": "add"}

old_memory = {"key": key, "value": old["value"], "category": old["category"]}
result = decide_memory_update(client, old_memory, memory)

if result["action"] == "update":
    update_memory(key, result["new_value"], memory.get("category"))

return result
```

### 4.6 记忆更新决策

**代码位置**：`memory/update.py` 的 `decide_memory_update()`

LLM 判断规则：
- 新信息与旧信息冲突 → `update`（用新值替换）
- 旧信息仍然有效 → `keep`（不改动）
- 只是新增信息 → `add`（但目前未处理）

**返回格式**：
```json
// update
{"action": "update", "reason": "冲突说明", "new_value": "新值"}

// keep
{"action": "keep"}
```

### 4.7 记忆 CRUD

**代码位置**：`memory/manager.py`

| 方法 | 作用 | 返回类型 |
|------|------|---------|
| `get_memory(key)` | 查单条记忆 | `{"value": ..., "category": ...}` 或 `None` |
| `get_all_memory()` | 查所有记忆 | `[(key, value), ...]` |
| `save_memory(memory)` | 写入/更新记忆 | 无返回 |
| `delete_memory(key)` | 删除单条 | 无返回 |
| `clear_memory()` | 清空所有 | 无返回 |
| `update_memory(key, value, category)` | 更新单条 | 无返回 |

**注意返回类型不一致**：`get_memory` 返回 dict，`get_all_memory` 返回 tuple 列表。目前只有 loader 用 `get_all_memory`，没出错，但要注意。

### 4.8 记忆加载与映射

**代码位置**：`memory/loader.py`

**KEY_MAP**：把中文记忆键映射为工具参数名
```python
KEY_MAP = {
    "用户城市": "destination",
    "居住地": "destination",
    "用户姓名": "user_name",
}
```

**加载流程**：
```python
# loader.py 第 18-24 行
def load_memory_into_state(state):
    memories = load_memory()           # 从数据库读取所有记忆
    for key, value in memories.items():
        mapped_key = KEY_MAP.get(key, key)  # 映射 key
        if not state.get(mapped_key):       # state 为空才填充
            state.set(mapped_key, value)
    return state
```

### 4.9 记忆钩子

**代码位置**：`agent.py` 的 `_memory_hook()`

**触发时机**：每轮对话结束后自动调用

**作用**：从用户输入中提取记忆，决策是否保存/更新

```python
# agent.py 第 176-190 行
def _memory_hook(self, user_input):
    memory = extract_memory(self.client, user_input)
    for item in memory.get("memories", []):
        result = remember(self.client, item)
        # 打印处理结果
```

---

## 第五部分：Agent 骨架设计

### 5.1 为什么要做骨架分离

**重构前**：main.py 包含所有逻辑，200+ 行，不可复用
**重构后**：骨架与业务分离，新业务只需继承 Agent 类

```
重构前:
└── main.py（一堆逻辑，不可复用）

重构后:
├── agent.py（通用骨架，可复用）
└── main.py（业务实现，只需 70 行）
```

### 5.2 Agent 类结构

**代码位置**：`agent.py`

```python
class Agent:
    def __init__(self, client, tools_schemas, tool_registry, system_prompt):
        # 初始化 LLM 客户端、工具列表、系统提示
    
    def run(self):
        # 主循环：选择模式 → 对话 → 记忆钩子
    
    def run_react(self, user_input):
        # ReAct 循环
    
    def run_plan_execute(self, user_input):
        # Plan-Execute 循环
        # 自动检测子类是否实现了 _extract_state 和 _plan
    
    def _execute_tool(self, tool_call):
        # 工具执行
    
    def _memory_hook(self, user_input):
        # 记忆处理
```

### 5.3 业务扩展点

**代码位置**：`main.py` 的 `TravelAssistant`

```python
class TravelAssistant(Agent):
    def _build_system_prompt(self):
        # 自定义系统提示（出行助手角色）
    
    def _extract_state(self, user_input):
        # 自定义状态提取（出行意图）
    
    def _plan(self, state):
        # 自定义任务规划（出行流程）
```

**扩展机制**：`run_plan_execute` 用 `hasattr` 检测子类是否实现了 `_extract_state` 和 `_plan`，实现了就用业务逻辑，没实现就退化为普通对话。

### 5.4 新项目复用

做新项目时，只需：

```python
from wayfinder.agent import Agent

class NewProjectAgent(Agent):
    def _build_system_prompt(self):
        return "你是一个XXX助手..."
    
    # 如果不需要 Plan-Execute，可不实现 _extract_state 和 _plan
```

---

## 第六部分：数据模型

### 6.1 TravelState

**代码位置**：`models.py`

```python
class TravelState(BaseModel):
    destination: str | None = None  # 目的地
    time: str | None = None         # 时间
    purpose: str | None = None      # 目的
```

**作用**：用 Pydantic 做数据校验，确保状态字段有效。

### 6.2 Plan

**代码位置**：`models.py`

```python
class Plan(BaseModel):
    need_info: list[str] = []  # 需要补充的信息
    steps: list[str] = []      # 执行步骤
```

**作用**：规范化 LLM 返回的计划格式。

---

## 第七部分：常见问题与解决方案

### Q1: LLM 返回非 JSON 格式怎么办？

**解决方案**：加 `response_format={"type": "json_object"}`

```python
# state_manager.py 第 28-30 行
response_format={
    "type": "json_object"
}
```

**注意**：extractor.py 和 update.py 目前没加，存在风险。

### Q2: state 被上一轮覆盖怎么办？

**解决方案**：每轮重置 state，记忆作为默认值

```python
# agent.py 第 113-115 行
state = AgentState()              # 新建空 state
load_memory_into_state(state)     # 从记忆加载默认值
```

### Q3: 记忆的 category 什么时候用？

**category 用于**：
- 分类管理（用户身份/偏好/习惯）
- 扩展搜索（按 category 查询）
- 未来做记忆管理（过期、优先级）

**目前**：只在写入和更新时传递，未做分类查询。

### Q4: 工具函数的参数怎么从 state 取？

**代码位置**：`executor.py`

```python
# executor.py 第 15-22 行
sig = inspect.signature(func)  # 获取函数签名
kwargs = {}
for param_name in sig.parameters:
    value = state.get(param_name)  # 从 state 取对应参数
    if value is not None:
        kwargs[param_name] = value
results[step] = func(**kwargs)   # 调函数
```

例如 `get_weather(destination: str)`，会从 state 里取 `destination` 字段。

### Q5: 怎么调试 Agent？

**打印关键信息**：
- `当前状态`：每轮 state 变化
- `计划`：LLM 规划的步骤
- `调用工具`：哪个工具被调了
- `执行结果`：工具返回了什么
- `记忆[action]`：记忆处理结果

这些打印在 agent.py 的 `run_plan_execute` 和 `_memory_hook` 里。

---

## 第八部分：待改进方向

### Bug 类

| 优先级 | 问题 | 位置 | 说明 |
|--------|------|------|------|
| 🔴 | .gitignore 未含 .env | .gitignore | 安全风险 |
| 🟡 | service.py 不处理 add 动作 | service.py L54 | 旧记忆存在时 add 会丢失 |
| 🟡 | run() 里 state 是死代码 | agent.py L30 | 创建了但没传 |
| 🟢 | extractor/update 缺 response_format | extractor.py, update.py | LLM 可能返回非 JSON |

### 功能扩展

- [ ] Lv3：会话记忆（多轮上下文压缩、摘要）
- [ ] Lv4：记忆管理（冲突解决、过期清理、add 动作合并）
- [ ] RAG：向量检索增强
- [ ] 错误处理：重试、超时、日志
- [ ] 单元测试：覆盖各模块

---

## 第九部分：核心概念速查表

| 概念 | 一句话解释 | 代码位置 |
|------|-----------|---------|
| Agent | LLM + 工具 + 循环 | agent.py |
| ReAct | 边想边做 | agent.py run_react() |
| Plan-Execute | 先规划后执行 | agent.py run_plan_execute() |
| Tool | LLM 可调用的函数 | tools/base.py |
| Tool Schema | 给 LLM 看的工具描述 | get_tool_schemas() |
| State | Agent 的上下文数据 | state.py |
| Memory | 持久化的用户偏好 | memory/ |
| Extract | 从对话中提取记忆 | extractor.py |
| Remember | 协调记忆读写 | service.py |
| KEY_MAP | 中文键→参数名映射 | loader.py |
| Skeleton | 通用 Agent 骨架 | agent.py |

---

## 第十部分：项目文件速查

### 按功能分组

**入口**：
- `main.py` — 业务入口、TravelAssistant 类
- `agent.py` — Agent 骨架基类

**状态**：
- `state.py` — AgentState 类
- `state_manager.py` — 状态提取
- `models.py` — TravelState、Plan 模型

**记忆**：
- `memory/database.py` — SQLite 连接、建表
- `memory/extractor.py` — 记忆提取（LLM）
- `memory/writer.py` — 记忆写入
- `memory/loader.py` — 记忆加载、KEY_MAP
- `memory/manager.py` — CRUD 管理
- `memory/service.py` — 记忆服务（协调 add/update/keep）
- `memory/update.py` — 记忆更新决策（LLM）

**工具**：
- `tools/base.py` — @tool 装饰器、Schema 生成
- `tools/registry.py` — 工具注册表
- `tools/weather.py` — 天气工具
- `tools/time.py` — 时间工具

**其他**：
- `planner.py` — 任务规划
- `executor.py` — 计划执行

---

> 💡 **复习方法**：忘记某个概念时，先查「概念速查表」找到代码位置，再对照代码阅读。每个模块都有注释和文档字符串。
