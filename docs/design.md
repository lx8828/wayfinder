# Tool Agent Runtime 设计文档

- 项目：智能出行 Agent Runtime
- 仓库：`first-agent`（本文档位于 `docs/design.md`，随代码一同版本化）
- 版本：v0.4
- 日期：2026-10-10
- 状态：Proposed（S0–S5 已实现，见 0.1）
- 术语：本文档版本为 v0.4；产品目标版本称 **v2**；旧手写实现称 **v1**（归入 `archive/handwritten_v1/`）
- 目标：围绕 Agent 编排、原生工具调用、短期/长期记忆和离线评测，把项目二从功能 Demo 升级为可测试、可观测、可回归的 Agent Runtime

---

## 0. 关键决策摘要

本节优先解决会决定代码量和实现方向的技术选型。

| ID | 决策 |
|---|---|
| A1 | ReAct 保留原生 `tools + tool_calls`，不使用自由 JSON 代替 Function Calling |
| A2 | 支持原生并行多工具调用，内部使用 `list[ToolCall]`，以 `call_id` 对齐结果 |
| A3 | v2.0 默认手动选择 ReAct/Plan，自动 Router 延后到可选阶段 |
| B1 | Plan 只保留 `depends_on + $steps.<id>.<field>` 一套寻址，删除 `output_key` |
| B2 | 每个工具必须同时声明输入 Schema 和输出 Schema |
| B3 | 新增独立 `SessionStore`，`pending_slots` 使用列表，不使用单字符串 |
| B4 | 短期记忆使用最近轮次 + `SessionSummary`，给出固定 token 预算和触发阈值 |
| B5 | LLM 只判断新旧记忆关系；`add/update/keep/ignore` 由确定性代码裁决 |
| B6 | v2.0 只支持单值 KV 记忆；多值记忆延后，不在 `value: str` 中塞 JSON |
| B7 | 删除 `confidence` 和 `version`；只保留有明确读取行为的字段 |
| B8 | `chat_json` 明确规定“解析 → 校验 → 错误回灌 → 重试 → 降级”状态机 |
| B9 | 架构图区分前置记忆读取和后置记忆写入 |
| C1 | Plan 评测 Tool Sequence；ReAct 评测 Tool Set 和合法拓扑序 |
| C2 | 确定性用例自动断言；开放式答案使用独立 LLM-as-Judge 测试集 |
| C3 | 引入 baseline 和 diff 回归机制 |
| C4 | 每个评测用例使用独立临时 SQLite，禁止共享污染 |
| C5 | 验收标准必须映射到测试或 eval case |
| C6 | 用例 `max_steps` 只能收紧全局预算，不能超过硬上限 |
| D1 | v1 先在独立快照中保持可运行，再迁移新入口 |
| D2 | 提供 v1 memory.db 一次性迁移脚本 |
| D3 | `providers/` 与 `tools/` 分离，工具定义和外部 IO 不重名混放 |
| D4 | v1 `models.py` 归档；v2 按领域拆分 schemas |
| D5 | 其他 Agent 仓库只做参考，不合并进 v2 |
| D6 | 明确包级依赖方向，禁止反向依赖和 import 副作用注册 |
| D7 | 每个 Phase 必须有可运行的退出条件 |
| D8 | 给出时间盒和最小演示命令 |
| A4 | ReAct 的 Thought 来自模型同一响应的 `content`（可空），不额外调用、不保存完整思维链；文档如实表述为 ReAct-style（Thought 可选） |
| B10 | 记忆迁移脚本必须包含 v1 中文 key/category → v2 英文的规范化映射 |
| B11 | Adjudicator 只在 key 已存在时调用；relation → action 映射必须 5 值全覆盖 |
| E1 | 区分“单次 `chat_json` 最大调用次数”与“单次 run 最大步数”两个上限，并定义超预算行为 |
| E2 | v1 归档使用独立环境与独立包名，避免同名包冲突和 `.env` 泄漏 |
| E3 | 删除死字段 `route_reason`，改为记录用户手动选择的 `mode_selection` |
| E | 修正章节命名、参数可选性、超时预算、Trace 隐私和 v1 可运行性 |

### 0.1 实现反馈与文档修正（S0–S4 已完成）

下列偏离是**实现过程中被真实约束逼出来的**，已同步到后文对应章节，此处集中登记。

| 编号 | 偏离 / 新增约束 | 原因 | 落点 |
|---|---|---|---|
| R1 | token 估算改用**字符数近似**（CJK 记 1 token、其他记 1/4 token，向上取整后乘安全系数），**不引入 `tiktoken`** | `tiktoken` 首次使用需联网下载 BPE 词表，会破坏「无 API Key 也能跑测试与离线评测」的硬约束 | 10.1 |
| R2 | `core/__init__.py` **不得**导入 `core.engine` | 否则形成导入环 `runtime.react → core.client → core.__init__ → core.engine → runtime.react` | 6 |
| R3 | `core/engine.py` 是**组合根**，唯一允许跨层 import 的模块 | 装配 ContextBuilder / SessionStore / MemoryStore / Engine 必须同时看到多个层 | 6 |
| R4 | 所有时间戳存 **ISO 文本**，不用 `DEFAULT CURRENT_TIMESTAMP` | SQLite 的 `CURRENT_TIMESTAMP` 是 UTC 且不带时区，比较与测试都不方便 | 9.2 |
| R5 | `WeatherResult` 放宽：`feels_like_c` 改为可选，新增 `temp_min_c` / `temp_max_c` | 未来 3 天预报没有「体感温度」，硬性必填会让预报查询必然失败 | 11.3 |
| R6 | 允许 `memory` 依赖 `core.db` | SessionStore 与 MemoryStore 需要共享同一套 SQLite 封装 | 6 |
| R7 | 新增 `core/db.py`（通用 SQLite 封装，**不含业务 schema**） | 同上 | 5 / 6 |
| R8 | 规范 key 从 5 个补到 **6 个**（新增 `dietary_preference`）；迁移映射表补「饮食偏好」「用户偏好」 | 用**真实 v1 归档库**验证映射时发现：原表漏了 v1 实际存在的「饮食偏好」，且 category 写的是「偏好」而真实数据是「用户偏好」。不补会导致抽取阶段**静默丢弃**饮食偏好（比 v1 还退步） | 12.2 / 18.2 |
| R9 | 记忆管线新增 `memory/migrate.py`（逻辑放包内），`scripts/migrate_memory_v1.py` 退化为薄 CLI | 迁移逻辑需要单元测试，脚本文件不适合被 import | 5 / 18.2 |

**实测发现（真实冒烟，S3–S4）**：DeepSeek 在带 `tool_calls` 的响应里**有时输出 `content`（Thought）、有时不输出**——同一模型对同类问题先后返回 `content=null` 与 `content="I'll check the current weather in Beijing for you."`。这确认了方案 A（Thought 可选）是唯一稳妥的选择。另注意 **Thought 语言不受输入语言约束**（中文输入得到英文 Thought），如需统一只能在 prompt 中显式要求。

**实测发现（会话回放）**：把持久化的 `assistant(tool_calls)` + `tool` 消息原样回放给真实 API 未触发协议错误，且第二轮模型能直接复用上一轮 Observation 作答而不重复调用工具。

**记忆管线的验证方法（S5，值得复用）**：记忆是「静默出错」风险最高的模块，因此除了常规单测，还额外做了两件事：

1. **变异测试**：手工注入两个历史缺陷（①「声称 `add` 但不写库」——v1 的原缺陷；②「无视 relation 一律覆盖」），
   确认测试会失败。前者被 **27 个**测试抓到，后者被 **14 个**抓到。测试有效才算测试。
2. **真实数据验证**：直接读 `archive/handwritten_v1/` 里的真实 v1 库跑映射，发现映射表缺 2 项（见 R8）。
   合成数据永远发现不了这种缺口。

围绕「数据不丢」写成了显式不变量：**行数单调不减**、**任何 key 的 value 永不为空**、
**无论 LLM 给出哪种 relation，已存在的 key 操作后依然存在**。

---

## 1. 背景

当前 v1 已经手工实现：

1. ReAct-style 工具调用循环。
2. Plan-Execute 顺序执行链路。
3. 基于 SQLite 的长期记忆。
4. 装饰器式工具注册和说明生成。
5. 命令行与单独 Web 入口。

v1 验证了基本机制，但存在以下问题：

- Plan 只有工具名，没有参数、依赖和结果引用。
- 工具参数隐式从 State 读取，没有显式契约。
- Plan-Execute 每轮重置 State，多轮追问会丢任务上下文。
- ReAct 工具 JSON、未知工具和工具异常没有保护。
- 记忆 `add/update/keep` 职责混乱，已有 key 时 `add` 可能丢数据。
- system prompt 只初始化一次，本轮新增记忆无法立即生效。
- 记忆提取、更新和规划使用裸 `json.loads()`。
- 没有 SessionStore、Trace、离线评测和自动化测试。
- `pyproject.toml` 的 CLI 入口指向占位函数，迁移后容易断。
- Web 端全局单例 Agent 会污染不同用户和会话。

### 1.1 v1 归档策略

v1 不直接删除，迁移流程如下：

1. 在迁移前创建 Git tag：`v1-handwritten`。
2. 复制一份完整的、自包含的 v1 到 `archive/handwritten_v1/`。
3. archive 内包含自己的 README、运行说明和独立依赖说明。
4. archive 不被 v2 主程序导入，不参与 v2 运行链路。
5. v1 通过独立命令运行，用于复现和学习。
6. v2 稳定后再决定是否永久删除。

归档目录示例：

```text
archive/handwritten_v1/
├── README.md
├── pyproject.toml
├── src/
│   └── first_agent/
│       ├── agent.py
│       ├── planner.py
│       ├── executor.py
│       ├── models.py
│       └── memory/
└── web.py
```

归档后仍可运行（**使用 archive 自带的独立 `.venv`，不安装进主线环境**）：

```bash
cd archive/handwritten_v1
uv sync          # 在该目录创建/使用独立 .venv
uv run python web.py
```

隔离要求：archive 的包名与主线同为 `first_agent`，**不得与主线安装进同一个 venv**（否则同名包互相覆盖）。若确需共用一个环境，必须把归档包改名为 `first_agent_v1` 并同步修改其内部 import。归档时同时排除 `.env`，避免密钥进入版本库。

---

## 2. 目标与非目标

### 2.1 目标

- ReAct 使用原生 Function Calling，不自造自由 JSON Action 协议。
- ReAct 显式保留 Thought（来源为模型同一响应的 `content`，可空），保持 ReAct 语义。
- 支持单工具、并行多工具和 Tool Observation 回传。
- Plan-Execute 支持参数、依赖、结果引用、重试和一次重规划。
- 区分 Working、Short-term 和 Long-term Memory。
- 每轮动态读取并注入最新长期记忆。
- 提供 SessionStore，支持 user/session 隔离。
- 全链路 Trace，可回放每一步规划和工具调用。
- 30～40 条离线评测用例，支持 baseline 回归。
- 无 API Key 时，单元测试和离线评测可运行。

### 2.2 非目标

- 不做 RAG。
- 不引入向量数据库。
- 不做多 Agent 协作。
- 不做生产级权限、计费和大规模并发。
- 不承诺线上 QPS 或真实用户规模。
- 不用 LangChain/LangGraph 黑盒替代手写核心。
- v2.0 不支持多值记忆和记忆历史版本。
- 不做 Thought 与 Action 分离的两次调用（即非教科书式 ReAct；Thought 可选，见 7.3）。

---

## 3. 设计原则

1. 原生协议优先：ReAct 的工具调用只使用 `tools + tool_calls`。
2. 显式契约优先：工具输入、输出和 Plan 依赖全部模型化。
3. 状态分离：Task、Session、Long-term Memory 分开存储。
4. 确定性裁决：LLM 判断事实关系，代码决定数据库动作。
5. 可回放：每轮 run 都能还原 plan、tool、memory 和 latency。
6. 失败可恢复：错误进入结构化降级路径，不直接中断。
7. 评测先于优化：没有 baseline 的改动不进入主线。
8. 控制复杂度：自动路由、多值记忆和多 Agent 均延后。

---

## 4. 目标架构

```text
CLI / API
    |
    v
SessionResolver ──> SessionStore
    |
    v
ContextBuilder <──(read)── Long-term Memory Store
    |                              ^
    v                              |
Runtime Engine                     | (write)
    |-- ReActEngine                |
    |-- PlanExecuteEngine          |
    v                              |
ToolRegistry ──> Tools ──> Providers
    |
    v
Answer ──> MemoryService ──────────┘
    |
    v
TraceLogger
```

组件命名统一：长期记忆的读写与裁决统一由 `MemoryService` 承担（不再出现 `MemoryPipeline` 别名）。`SessionResolver` 负责解析 `user_id / session_id`（缺省时生成新 session），其底层是 `SessionStore`。

关键时序：

1. 本轮开始时，先从 Long-term Memory 读取并注入 ContextBuilder。
2. 本轮结束后，再由 `MemoryService` 执行抽取、关系判断和写入。
3. 下一次对话开始时读取最新记忆。
4. Trace 同时记录读记忆快照和后置写记忆动作，但不混为同一次读操作。

---

## 5. 目录结构与职责

```text
src/first_agent/
├── core/
│   ├── client.py          # ✅ LLM 客户端与 chat_json 状态机
│   ├── context.py         # ✅ ContextBuilder（动态注入 + 字符预算）
│   ├── db.py              # ✅ 通用 SQLite 封装（R7，无业务 schema）
│   ├── session.py         # ✅ SessionStore / SessionResolver
│   ├── tracing.py         # ✅ TraceLogger 与脱敏
│   └── engine.py          # ✅ 组合根 ToolAgentRuntime（唯一跨层 import 点）
├── schemas/
│   ├── tool.py            # ✅ ToolSpec / ToolCall / ToolResult / NormalizedAssistantTurn
│   ├── plan.py            # ✅ Plan / PlanStep / $steps 引用解析
│   ├── memory.py          # ✅ MemoryCandidate / MemoryRelation / UserProfile
│   └── session.py         # ✅ WorkingState / SessionSummary
├── providers/
│   ├── base.py            # ✅ Provider 协议与异常体系
│   ├── fake.py            # ✅ Fake provider（失败注入 / 空结果）
│   └── weather.py         # ✅ QWeatherProvider（实况 + 3 天预报）
├── tools/
│   ├── registry.py        # ✅ 显式注册表
│   ├── executor.py        # ✅ 校验 / 超时 / 重试 / 有界并发
│   └── builtin/
│       ├── time.py        # ✅ get_current_time
│       ├── weather.py     # ✅ get_weather
│       ├── travel.py      # ⏳ S6
│       └── calendar.py    # ⏳ S6
├── runtime/
│   ├── react.py           # ✅ ReActEngine
│   └── plan_execute.py    # ⏳ S6
├── memory/
│   ├── store.py           # ✅ MemoryStore（确定性动作裁决）
│   ├── extractor.py       # ✅ 记忆抽取（LLM，别名规范化 + 逐条容错）
│   ├── adjudicator.py     # ✅ 关系裁决（LLM，失败保守跳过）
│   ├── service.py         # ✅ 三层编排（抽取 → 裁决 → 落库）
│   └── migrate.py         # ✅ v1 → v2 迁移（R9）
├── evals/
│   ├── scripted.py        # ✅ ScriptedLLM
│   ├── case.py            # ✅ EvalCase / 判定 / 加载
│   ├── runner.py          # ✅ RunOutcome / CaseCheck / run_case
│   ├── metrics.py         # ✅ 指标与 baseline 门禁
│   ├── adapters.py        # ✅ 真引擎 → RunOutcome
│   ├── cases.jsonl        # ✅ 5 条种子（S7 扩到 30～40）
│   └── baseline.json      # ⏳ S7
└── main.py                # ✅ CLI（chat --mode react）

tests/                     # ✅ 258 个离线测试（无 API Key 可跑）
var/                       # ✅ 本地 SQLite 与 Trace（已 gitignore）

scripts/
└── migrate_memory_v1.py   # ✅ v1→v2 迁移 CLI（薄包装，逻辑在 memory/migrate.py）

archive/
└── handwritten_v1/        # ✅ v1 冻结快照（独立 .venv，含 ARCHIVE.md）
```

> 图例：✅ 已实现　⏳ 待实现

职责边界：

- `tools/builtin/`：工具定义、输入输出模型、业务规则。
- `providers/`：外部 API、数据库、日历等 IO 实现。
- `tools/` 不实现网络访问细节。
- `core/`：基础设施，不放业务 Agent 逻辑。
- `runtime/`：ReAct 和 Plan-Execute 控制流。
- `memory/`：长期记忆抽取、裁决和持久化。
- `evals/`：离线评测和回归。

---

## 6. 分层依赖规则

依赖方向必须单向：

```text
schemas                              （最底层，不依赖任何其他包）
  ↑
  ├── providers
  ├── core ──> schemas
  ├── tools ──> schemas + providers
  ├── memory ──> schemas + core.db
  ├── runtime ──> schemas + core + tools
  └── core.engine（组合根）──> runtime + memory + tools + core
                                    ↑
                                  main
```

禁止：

- `schemas` 依赖 runtime、tools、memory、core。
- `providers` 依赖 schemas / tools / memory / runtime。
- `tools` 依赖 memory 或 runtime。
- `memory` 依赖 tools 或 runtime。
- `core` 依赖 memory / runtime / tools（**组合根除外**，见下）。
- `tools/__init__.py` 通过 import 副作用注册工具。
- 循环依赖或从 archive 反向导入主线。

**组合根豁免（R3）**：`core/engine.py` 是唯一的装配点，它必须同时依赖
`runtime` / `memory` / `tools` / `core`。因此它**不能被 `core/__init__.py` 急切导入**
（R2），否则会形成导入环：

```text
runtime.react → core.client → core.__init__ → core.engine → runtime.react
```

门面必须显式导入：

```python
from first_agent.core.engine import ToolAgentRuntime
```

回归护栏：`tests/test_import_order.py` 用独立解释器枚举多种导入顺序，防止该环再次出现。

补充（R6）：`memory` 允许依赖 `core.db`，以便与 SessionStore 共享同一个 SQLite 封装。

工具注册改为显式：

```python
registry.register(get_weather_spec)
registry.register(get_current_time_spec)
```

---

## 7. ReAct 与原生 Function Calling

### 7.1 唯一通道

ReAct 的模型请求必须使用：

```python
client.chat.completions.create(
    model=...,
    messages=messages,
    tools=tool_schemas,
    tool_choice="auto",
)
```

不允许把 `AgentDecision` 作为模型输出协议。

### 7.2 内部归一化

模型响应只在 Runtime 内归一化为：

```python
class ToolCall(BaseModel):
    call_id: str
    tool_name: str
    arguments: dict


class NormalizedAssistantTurn(BaseModel):
    # thought 即原生响应的 content：模型同一次响应里的自然语言说明，可为空
    thought: str | None = None
    tool_calls: list[ToolCall] = []
```

`NormalizedAssistantTurn` 不是模型输出契约，而是 Runtime 对原生响应的内部投影。

### 7.3 Thought 与 ReAct 语义

原生 Function Calling **不强制模型输出 Thought**。v0.1 曾用带 `thought_summary` 的 `AgentDecision` 承载 Thought，v0.2 为保留原生协议删除了该结构，导致 Thought 一并消失，7.x 的主循环退化为 **Act-only**。本节明确 v2.0 的 Thought 来源与缺失行为：

- Thought 的唯一来源：模型**同一次响应**里的 `content` 字段（可与 `tool_calls` 同时返回）。
- 禁止为获取 Thought 额外发起 LLM 调用（不得为此退回到 v0.1 的自由 JSON Action 协议）。
- Runtime 将 `content` 归一化为 `NormalizedAssistantTurn.thought`，与 `tool_calls` 一并写入消息和 Trace。
- 模型本轮未输出 `content`（DeepSeek 在有 `tool_calls` 时常见返回 null）时，`thought = None`，**不视为错误**，循环照常继续。
- 完整思维链不入库：Trace 中 `thought` 截断到 `MAX_THOUGHT_CHARS`（默认 500）。
- 是否向用户展示 Thought 由 `THOUGHT_VISIBLE` 控制（默认 false，仅 Trace 可见；CLI 可加 `--show-thought`）。

若使用 reasoning 模型（如 `deepseek-reasoner`），可把 `reasoning_content` 作为 Thought 来源，但必须遵守其不落库的合规要求，且 v2.0 不默认启用。

因此 v2.0 的准确表述是 **ReAct-style（Thought 可选）**。若评测要求教科书式 ReAct（Thought 必填），必须把单次响应拆成“先出 Thought、再出 Action”的两次调用，本版本明确不做。

> 已选定方案 A：Thought 可选、原生单次调用；不做两次调用的严格 ReAct。

实现反馈（真实冒烟实测）：

- DeepSeek 在带 `tool_calls` 的响应里**有时给 `content`、有时不给**：同一模型对同类问题先后返回
  `content=null` 与 `content="I'll check the current weather in Beijing for you."`。
  这确认了「Thought 可选」是唯一稳妥的选择，**不能**把 Thought 当作必填字段。
- 没有 Thought 时，**不写 `react_thought` 事件**，避免 Trace 里出现 `thought: ""` 的噪音。
- Thought 语言不受输入语言约束（中文输入可能得到英文 Thought）；如需统一，只能在 prompt 中显式要求。

### 7.4 并行工具调用

- 一次响应允许多个 `tool_calls`。
- 工具执行需要有界并发，默认并发数为 4；因工具基于同步 `httpx`，使用 `concurrent.futures.ThreadPoolExecutor(max_workers=4)` 实现，不做 async 改造。
- 并发下各工具的 `timeout_s` 独立生效，整体等待时间取其中最长者；任一工具超时只影响该 `ToolResult`，不阻塞其他工具。
- 每个 ToolResult 必须带 `call_id`。
- 回传消息必须与原生协议配对，且**顺序固定为 assistant → tool**：必须先追加一条 `assistant` 消息（携带本轮 `thought`（即原生 `content`）与 `tool_calls`），再追加每个对应的 `tool` 消息。缺少 assistant 消息会导致协议错误或模型丢失上下文。

```json
{"role": "assistant", "content": "需要先确认时间", "tool_calls": [...]}
{"role": "tool", "tool_call_id": "call_123", "content": "{...}"}
```

- ToolResult 顺序不影响最终结果，但 Trace 保留原始调用顺序。
- ReAct 不评测 JSON Validity Rate；该指标只用于 Plan 和 Memory 的结构化输出。

### 7.5 ReAct 主循环

```text
构建上下文
→ 原生 Function Calling
→ 归一化 Thought（= 原生 content，可为空）并写入 Trace
→ 如果有 tool_calls：
    追加 assistant（thought + tool_calls）
    → 校验参数
    → 执行工具
    → 追加 tool messages（call_id 配对）
    → 下一轮
→ 如果没有 tool_calls：
    以 content 作为最终答案返回
```

保护规则：

- 最多 5 步；
- 同一工具 + 同一参数连续调用两次时拦截；
- 参数非法时不执行；
- 工具失败返回结构化错误；
- 最终答案必须有 Observation 支撑。

---

## 8. Plan-Execute 设计

### 8.1 Plan 结构

```python
class PlanStep(BaseModel):
    id: int
    tool: str
    arguments: dict = {}
    depends_on: list[int] = []


class Plan(BaseModel):
    need_info: list[str] = []
    steps: list[PlanStep] = []
    reason: str = ""
```

明确规则：

- 删除 `output_key`。
- 删除 `$steps.<output_key>.<field>`。
- 数据引用只使用 `$steps.<id>.<field>`。
- `depends_on` 是执行顺序的唯一来源。
- 如果 arguments 引用了 step N，则 N 必须出现在 `depends_on` 中。
- 执行顺序由 `depends_on` 拓扑排序得到，不依赖 LLM 列表顺序。

示例：

```json
{
  "steps": [
    {
      "id": 0,
      "tool": "resolve_datetime",
      "arguments": {
        "expression": "明天下午",
        "timezone": "Asia/Shanghai"
      },
      "depends_on": []
    },
    {
      "id": 1,
      "tool": "get_weather",
      "arguments": {
        "city": "重庆",
        "date": "$steps.0.date"
      },
      "depends_on": [0]
    }
  ]
}
```

### 8.2 Plan 执行

```text
读取 Working State
→ 合并 Long-term Memory 默认值
→ 生成 Plan
→ Schema 校验
→ 引用校验
→ DAG / 拓扑排序
→ 执行步骤
→ 写回 Working State
→ 失败时 retry / fallback / replan 一次
→ 汇总回答
```

“引用校验”包含三步，缺一不可：

1. 引用的 step id 存在；
2. 被引用的 step id 出现在本 step 的 `depends_on` 中；
3. 引用的 `field` 存在于被引用工具 `output_model` 的字段中。

任一步不满足则拒绝该 Plan，进入重规划或追问。

### 8.3 多轮追问

Working State 由 SessionStore 持久化。

```python
class WorkingState(BaseModel):
    user_id: str
    session_id: str
    destination: str | None = None
    origin: str | None = None
    date: str | None = None
    purpose: str | None = None
    transport_mode: str | None = None
    pending_slots: list[str] = []
    resolved_values: dict = {}
```

用户只补充一个槽位时，必须合并旧 Working State，不能每轮清零。

---

## 9. SessionStore 设计

SessionStore 是独立组件，不由 Agent 全局单例承载。

### 9.1 存储方式

- 测试：内存 SQLite。
- 本地/Web：文件 SQLite。
- 所有查询必须带 `user_id + session_id`。

### 9.2 表

```sql
-- 说明（R4）：所有时间戳统一存 Python 侧生成的 ISO 文本（如 2026-10-10T03:12:31），
-- 不使用 SQLite 的 CURRENT_TIMESTAMP（UTC 且不带时区，比较与测试都不方便）。

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE working_states (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE turns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    turn_index       INTEGER NOT NULL,
    user_input       TEXT NOT NULL,
    assistant_output TEXT NOT NULL,
    -- 该轮完整消息序列（含 assistant tool_calls 与 tool observation），供 ReAct 多轮回放
    messages_json    TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(session_id, turn_index)
);

CREATE TABLE summaries (
    session_id           TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    covered_through_turn INTEGER NOT NULL,
    summary              TEXT NOT NULL,
    token_count          INTEGER NOT NULL,
    updated_at           TEXT NOT NULL
);
```

---

## 10. 短期记忆与摘要

### 10.1 硬性预算

```text
MAX_CONTEXT_TOKENS = 24000
SUMMARY_TRIGGER_TOKENS = 16000
RECENT_TURNS_KEEP = 8
OUTPUT_RESERVE_TOKENS = 4000
```

token 估算使用**字符数近似**（R1）：CJK 字符记 1 token、其他字符记 1/4 token，**向上取整**后乘安全系数（默认 1.2），再与阈值比较。选它而不是 `tiktoken` 的原因：`tiktoken` 首次使用需联网下载 BPE 词表，会破坏「无 API Key 也能运行测试与离线评测」这一硬约束。用 `ceil` 而非截断，是为了保证安全系数**只高估不低估**（`2 * 1.2 = 2.4` 截断成 2 会让安全系数失效）。`MAX_CONTEXT_TOKENS` 是 v2 自设预算，与模型上下文上限无关，已预留 `OUTPUT_RESERVE_TOKENS` 与 tool schema 占用。阈值必须配置化，不能写死在业务代码中。

两个轮次阈值的关系：空闲时保留全部原文；一旦未摘要轮次达到 `SUMMARY_TRIGGER_TOKENS` 或超过 16 轮，就把“早于最近 `RECENT_TURNS_KEEP` 轮”的部分压缩进 `SessionSummary`，最近 8 轮始终保留原文。即 8 是“保底原文窗口”，16 是“触发压缩的窗口”，两者不冲突。

实现状态：S4 只落地「最近 N 轮窗口 + 字符预算裁剪」；**摘要生成（10.3 的 LLM 调用）尚未实现**，`summaries` 表已建好且读取路径已通，写入随记忆/评测阶段接入。

### 10.2 SessionSummary

```python
class SessionSummary(BaseModel):
    session_id: str
    covered_through_turn: int
    summary: str
    token_count: int
    updated_at: str
```

### 10.3 触发与幂等

- 当未摘要轮次的估计 token 超过 `SUMMARY_TRIGGER_TOKENS`，或未摘要轮次超过 16 轮时触发。
- 只摘要 `covered_through_turn` 之后的轮次。
- 摘要结果持久化到独立的 `summaries` 表（见 9.2），以 `session_id` 为键、`covered_through_turn` 为版本。
- 摘要失败不阻塞当前回答，保留最近原始轮次并记录 Trace。
- 摘要 LLM 调用必须记录 latency 和 usage。
- 摘要幂等键为 `session_id + covered_through_turn`。

---

## 11. 工具契约

### 11.1 ToolSpec

每个工具同时声明输入与输出 Schema：

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable
    timeout_s: float = 5.0
    max_retries: int = 1
    side_effect: bool = False
```

### 11.2 ToolResult

```python
class ToolResult(BaseModel):
    call_id: str
    ok: bool
    tool_name: str
    data: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0
```

`data` 的执行流程固定为：`handler 返回原始值 → output_model 校验 → model_dump()`，因此 `data` 始终是**已校验对象的 dict 形式**，不允许直接写入未校验的原始字典。`$steps.<id>.<field>` 的 `field` 即此 dict 的键，校验时以对应 `output_model` 的字段名为准。

### 11.3 输出模型示例

```python
class WeatherResult(BaseModel):
    city: str
    date: str
    condition: str
    temperature_c: float
    # 实况有体感温度；3 天预报没有，故为可选（R5）
    feels_like_c: float | None = None
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    humidity: int
    source: str


class TimeResult(BaseModel):
    timezone: str
    iso_time: str


class TravelTimeResult(BaseModel):
    origin: str
    destination: str
    mode: str
    duration_minutes: int


class CalendarResult(BaseModel):
    date: str
    events: list[str]
```

### 11.4 参数可选性

- 必填字段由 Pydantic `is_required()` 决定。
- 有默认值的字段不进入 required。
- JSON Schema 生成必须反映 default、description、enum 和类型。
- `get_weather(date: str | None = None)` 的 `date` 可选，默认今天。
- `get_current_time(timezone: str = "Asia/Shanghai")` 的 timezone 可选。

### 11.5 内置工具集合

| 工具 | 输入 | 输出 | 说明 | 状态 |
|---|---|---|---|---|
| `get_current_time` | `timezone` | `TimeResult` | 单参数、确定性；时钟可注入 | ✅ S3 |
| `get_weather` | `city, date?` | `WeatherResult` | 外部依赖（当天实况 / 未来 3 天预报），可失败 | ✅ S3 |
| `resolve_datetime` | `expression, timezone` | `ResolvedTimeResult` | 处理“明天下午”，需要 LLM 解析 | ⏳ S6 |
| `estimate_travel_time` | `origin, destination, mode` | `TravelTimeResult` | 多参数、可形成依赖 | ⏳ S6 |
| `get_calendar_events` | `user_id, date` | `CalendarResult` | 本地数据、可离线测试 | ⏳ S6 |

### 11.6 地点解析

硬编码地点必须删除。

```text
当前用户输入
> Working State
> Long-term Memory.home_city
> 追问用户
```

工具只接受城市参数，不读取默认地点。

---

## 12. 记忆模型

### 12.1 统一存储模型

Long-term Memory 的规范存储是单值 KV。

```python
class MemoryCandidate(BaseModel):
    user_id: str
    key: str
    value: str
    category: Literal[
        "user_identity",
        "preference",
        "habit",
        "long_term_plan"
    ]
    source: str = "conversation"


class MemoryRelation(BaseModel):
    # 只描述“新值与同 key 旧值的关系”，不含“是否值得保存”（那是 Extractor 的职责）
    relation: Literal[
        "same",
        "conflict",
        "complementary",
        "unsupported"
    ]
    reason: str = ""
```

`home_city` 是 key，不是对象属性。需要 typed 访问时，由 `UserProfile.from_memories()` 生成只读视图。

### 12.2 单值与多值

v2.0 只支持单值 KV：

- `home_city`
- `timezone`
- `transport_mode`
- `user_name`
- `dietary_preference`（S5 用真实数据补入，见 R8）
- `long_term_plan`

多值记忆延后。禁止把 JSON 数组塞进 `value: str`。如果确实需要多值，新增 `memory_values` 表，而不是修改当前语义。

### 12.3 职责分离

三层职责严格单向，不允许串味：

**Extractor（判断“值不值得存”）**

- 输入：用户输入。
- 输出：`MemoryCandidate` 列表，或空列表。
- 不产出 `MemoryCandidate` 等价于 `ignore`；该判定到此为止，下游不再重复判断。

**Adjudicator（判断“与旧值什么关系”）**

- **只在 key 已存在（有旧值）时才被调用**；无旧值时不调用，避免无意义的 LLM 调用。
- 输入：`new MemoryCandidate` + 同 key 的旧 value。
- 输出：`MemoryRelation`（`same` / `conflict` / `complementary` / `unsupported`）。
- 不输出 `add/update/keep/ignore`。

**MemoryService（确定性决定数据库动作）**

| 前置条件 | relation | 动作 |
|---|---|---|
| 无旧值 | 不适用（不调用 Adjudicator） | `add` |
| 有旧值 | `same` | `keep` |
| 有旧值 | `conflict` | `update` |
| 有旧值 | `complementary` | `skip`（v2.0 单值 KV 无法承载补充信息），记录 Trace |
| 有旧值 | `unsupported` | `skip`（结构无法落到单值 key），记录 Trace |

其中 `add / keep / update / skip` 全部由代码裁决，**LLM 不再输出这四个动作**；`ignore` 只可能由 Extractor 阶段产生。

### 12.4 数据库字段

```sql
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    category TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'conversation',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(user_id, key)
);
```

字段行为：

- `source`：在 Memory Viewer 和 Trace 中读取。
- `last_seen_at`：`keep` 或读取注入时更新。
- 删除 `confidence`。
- 删除 `version`，v2.0 不做历史表。
- 不做字段只写不读的“伪设计”。

### 12.5 每轮注入

每轮开始必须重新读取记忆：

```text
业务规则
+ UserProfile
+ Working State
+ Session Summary
+ 最近 N 轮原文
```

不能复用初始化时的 system prompt。

---

## 13. JSON 输出状态机

DeepSeek 的 `json_object` 不保证字段类型和完整性，因此 `chat_json` 必须定义重试状态机：

```text
第 1 次调用
→ JSON parse
→ Pydantic validate
→ 成功：返回

失败：
→ 把原始输出、Pydantic error 和修复要求追加到消息
→ 第 2 次调用
→ 再校验
→ 仍失败：降级
```

规则：

- `response_format={"type": "json_object"}` 必须启用。
- `chat_json` 默认最多 2 次调用，包括第一次。
- 每次调用 timeout 30 秒。
- 重试次数、错误和 latency 写入 Trace。
- Plan 失败：降级到 ReAct 或追问用户。
- Memory 失败：no-op，不污染数据库。
- Adjudicator 失败：保留旧记忆。
- Structured Output Validity Rate 只衡量 Plan 和 Memory。

---

## 14. 超时、重试与预算

| 项 | 默认值 |
|---|---:|
| LLM 单次 timeout | 30 秒 |
| **单次 `chat_json` 最大调用次数** | 2（含首次） |
| Tool timeout | 5 秒 |
| Tool 最大重试 | 1 |
| ReAct 最大步数 | 5 |
| 单次 run 最大步数硬上限 | 8 |
| 单次 run 总 latency 预算 | 60 秒 |
| 并行工具并发 | 4 |
| `MAX_THOUGHT_CHARS`（Trace 中 Thought 截断长度） | 500 |
| `THOUGHT_VISIBLE`（是否向用户展示 Thought） | false |
| 上下文估算安全系数 | 1.2 |

注意：**“单次 `chat_json` 最大调用次数”与“单次 run 最大步数”是两个不同上限**。前者限制单个结构化输出请求的重试（见 13 节），后者限制整个 ReAct/Plan 循环的步数；二者不可混用，也不相乘计入同一预算。

优先级：

```text
effective_max_steps = min(case.max_steps or global_max_steps, hard_cap)
```

用例不能把 max_steps 放大到 8 以上。

### 14.1 超预算行为

总 latency 预算是硬性熔断点。当单次 run 累计耗时达到 `60s`：

1. 停止发起新的 LLM 调用和工具调用（进行中的调用允许收敛）。
2. 若已有可用 Observation，返回“基于已获取信息的部分结果 + 说明”。
3. 若没有任何可用 Observation，返回可解释的失败提示。
4. Trace 记录 `status = "budget_exceeded"`、已用耗时和中断位置。

不允许出现“预算耗尽但继续跑”或“静默抛异常”两种行为。

---

## 15. Trace 与隐私

Trace 写入：

```text
var/traces/YYYY-MM-DD.jsonl
```

字段：

- run_id、user_id_hash、session_id_hash
- mode、mode_selection（v2.0 为用户手动选择，自动 Router 未启用，因此不记录 route_reason）
- thought（每步的 Thought 摘要，超长截断）
- memory_read_snapshot
- plan
- tool_calls
- tool_results
- memory_relations
- memory_actions
- summary_call
- retries
- final_answer
- latency、usage、status

隐私规则：

- `var/traces/` 加入 `.gitignore`。
- 默认 `TRACE_MODE=safe`，对 user_id、session_id、电话、邮箱做 hash/脱敏，并对记忆值中的姓名、地址、电话、邮箱做掩码（如 `张*`、`13****1234`）。
- user_id / session_id 仅在 Trace 中 hash；数据库（`memories`、`sessions`）中保持明文，属有意设计（DB 为本地文件，Trace 可能被外发），文档与代码注释需明确该不一致是刻意的。
- 本地调试可以显式使用 `TRACE_MODE=full`。
- 单文件按天轮转。
- 保留 7 天或最多 1000 条 run，取先到者。
- 错误日志不得记录 API Key。

---

## 16. 评测设计

### 16.1 评测用例规模

首版 30～40 条：

| 类型 | 数量 |
|---|---:|
| 单工具调用 | 8 |
| 双工具组合 | 6 |
| 三工具依赖链 | 5 |
| 缺少参数追问 | 4 |
| 工具失败与降级 | 4 |
| 记忆 add/update/keep/skip/ignore | 6 |
| 无关问题不调用工具 | 3 |

### 16.2 用例格式

```json
{
  "case_id": "plan_001",
  "mode": "plan",
  "input": "我明天下午想去公园，天气和日程允许吗？",
  "initial_memory": {"home_city": "重庆"},
  "expected_tool_set": [
    "resolve_datetime",
    "get_weather",
    "get_calendar_events"
  ],
  "expected_tool_sequence": [
    "resolve_datetime",
    "get_weather",
    "get_calendar_events"
  ],
  "allowed_topological_orders": [],
  "expected_arguments": {
    "get_weather": {"city": "重庆"}
  },
  "expected_memory_delta": {},
  "max_steps": 5,
  "assertions": [
    {"type": "contains", "target": "final_answer", "value": "天气"}
  ]
}
```

三个工具字段不可混用，按 `mode` 决定以谁为准：

| 字段 | 适用 mode | 语义 |
|---|---|---|
| `expected_tool_sequence` | `plan` | 必须**按序**匹配，且序列须属于合法拓扑序 |
| `allowed_topological_orders` | `plan` | 可选；当存在多个合法拓扑序时，允许列表中的任一序 |
| `expected_tool_set` | `react` | 只比较**集合**，不约束顺序 |
| `expected_arguments` | 两者 | 期望参数（值可引用 `initial_memory`，如 `home_city → city`） |

- `plan` 用例若同时填写 `expected_tool_sequence` 与 `allowed_topological_orders`，**以 `allowed_topological_orders` 为准**；`expected_tool_sequence` 视为默认拓扑序（写入基线用）。
- `expected_status` 为可选字段，只表示运行状态，**不能代表答案正确**。

### 16.3 评测口径

- Plan：比较有序 `Tool Sequence`（受上表规则约束）。
- ReAct：比较 `Tool Set`、参数和额外调用，不强制唯一顺序。
- 确定性答案使用断言。
- 开放式答案另建 `judge_cases.jsonl`，使用独立 LLM-as-Judge，并按 judge 准确率单独报告。

### 16.4 数据库隔离

- 每个 case 使用独立临时 SQLite 或 `:memory:`。
- 用例结束后销毁 DB。
- 禁止在 cases 之间共享 memory.db。
- 测试并发执行时，每个 case 必须有唯一 session_id。

### 16.5 Baseline 回归

目录：

```text
evals/
├── baseline.json
├── current.json
└── diff.md
```

流程：

1. 固定 ScriptedLLM 和 Fake Tools 跑**稳定指标**。
2. 写入 `baseline.json`。
3. 改动后生成 `current.json`。
4. 产出 `diff.md`。
5. 稳定指标**恶化**超过 1pt：失败（硬门禁）。
6. 真 LLM **波动指标**恶化超过 3pt：警告，需要人工复核。

门禁按“指标恶化”判定，而非“数值下降”：`Tool Selection Accuracy` 等越高越好的指标，下降即恶化；`Extra Tool Call Rate`、`Average Steps` 等越低越好的指标，上升即恶化。每个指标必须标注方向。

**稳定指标**（ScriptedLLM + Fake Tools 下确定性，进硬门禁）：
Tool Selection Accuracy、Tool Sequence Accuracy、Argument Accuracy、Structured Output Validity Rate、Memory Decision Accuracy、Extra Tool Call Rate、Average Steps。

**波动指标**（依赖真 LLM，只告警）：
Task Success Rate、Cross-session Memory Recall、Run-to-run Consistency、Average Latency。

保存：

- git commit
- model
- prompt version
- tool schema version
- case set hash

### 16.6 指标

| 指标 | 口径 |
|---|---|
| Tool Selection Accuracy | 选中工具（集合）正确的用例占比 |
| Tool Sequence Accuracy | Plan 用例中工具序列（含合法拓扑序）正确的占比 |
| Argument Accuracy | 期望参数键值全部命中的占比 |
| Structured Output Validity Rate | Plan / Memory 的 `chat_json` 一次或重试后校验通过率 |
| Extra Tool Call Rate | 超出 `expected_tool_set` 的多余调用占比 |
| Memory Decision Accuracy | 确定性用例中最终 DB 动作（add/update/keep/skip）正确的占比 |
| Cross-session Memory Recall | 跨会话可恢复的长期记忆占比 |
| Average Steps | 平均循环步数（越低越好） |
| Average Latency | 平均单次 run 耗时（越低越好） |
| Task Success Rate | **仅对 `judge_cases.jsonl` 的开放式用例**，按 LLM-as-Judge 判定正确的占比；确定性用例不计入本指标（用 `assertions` 断言代替） |
| Run-to-run Consistency | 同一用例真 LLM 重复 3 次的结论一致率 |

---

## 17. 验收与测试映射

下表在实现过程中改为**真实测试标识**，便于直接定位。

| 验收项 | 测试 / eval | 状态 |
|---|---|---|
| 原生多工具调用可正确配对 | `tests/test_react_engine.py::test_parallel_tool_calls_pair_by_call_id` | ✅ |
| Thought 归一化，缺失时不报错 | `tests/test_react_engine.py::test_thought_is_optional` | ✅ |
| 无 Thought 时不产生 Trace 噪音 | `tests/test_react_engine.py::test_empty_thought_is_not_traced` | ✅ |
| assistant → tool 消息配对完整 | `tests/test_react_engine.py::test_assistant_message_precedes_tool_message` | ✅ |
| 非法参数不执行工具 | `tests/test_tool_executor.py::test_invalid_arguments_are_not_executed` | ✅ |
| 未知工具返回结构化错误 | `tests/test_tool_executor.py::test_unknown_tool_returns_structured_error` | ✅ |
| 工具超时被强制 | `tests/test_tool_executor.py::test_timeout_is_enforced` | ✅ |
| 瞬时失败重试 / 非瞬时不重试 | `tests/test_tool_executor.py::test_transient_failure_is_retried_then_succeeds` | ✅ |
| 工具失败可降级 | `tests/test_react_engine.py::test_provider_failure_is_surfaced_to_model` | ✅ |
| 多轮追问保留状态 | `tests/test_runtime_engine.py::test_working_state_pending_slots_survive_across_turns` | ✅ |
| 同会话立即使用新记忆 | `tests/test_runtime_engine.py::test_new_memory_is_injected_on_the_next_turn` | ✅ |
| 会话之间不串数据 | `tests/test_runtime_engine.py::test_sessions_do_not_leak_between_session_ids` | ✅ |
| 评测用例之间不串记忆 | `tests/test_eval_adapters.py::test_cases_are_isolated_by_case_id` | ✅ |
| key 已存在时 add 不丢数据 | `tests/test_memory_store.py::test_relation_mapping_is_deterministic` | ✅ |
| **记忆行数单调不减、value 永不为空** | `tests/test_memory_service.py::test_sequence_never_loses_rows_or_values` | ✅ |
| **任意 relation 都不会弄丢已有 key** | `tests/test_memory_service.py::test_existing_key_survives_every_relation` | ✅ |
| 无旧值时不调 Adjudicator | `tests/test_memory_service.py::test_first_write_is_add_and_skips_adjudicator` | ✅ |
| 抽取失败 → no-op 不污染库 | `tests/test_memory_service.py::test_extraction_failure_writes_nothing` | ✅ |
| 迁移幂等 | `tests/test_memory_migration.py::test_migration_is_idempotent` | ✅ |
| 真实 v1 key/category 全部可映射 | `tests/test_memory_migration.py::test_real_v1_key_and_category_pairs_are_all_recognized` | ✅ |
| 记忆失败不影响回答 | `tests/test_runtime_memory_hook.py::test_memory_failure_never_breaks_the_answer` | ✅ |
| JSON 校验失败可恢复 | `tests/test_chat_json.py::test_chat_json_retries_and_feeds_error_back` | ✅ |
| 导入顺序不产生循环导入 | `tests/test_import_order.py` | ✅ |
| 引擎异常不炸整批评测 | `tests/test_evals_harness.py::test_run_case_turns_engine_crash_into_failure` | ✅ |
| baseline 门禁按「指标恶化」判定 | `tests/test_evals_harness.py::test_compare_metrics_is_direction_aware` | ✅ |
| Plan 依赖可拓扑排序 | `test_plan_topological_sort` | ⏳ S6a |
| 引用必须声明 depends_on | `test_plan_reference_requires_dependency` | ⏳ S6a |
| 超预算可解释退出 | `test_run_budget_exceeded` | ⏳ S6b |
| 跨会话记忆恢复 | `tests/test_memory_store.py` + `tests/test_runtime_engine.py` | ✅（评测用例样本待 S7 扩到 30～40 条） |

---

## 18. 迁移方案

### 18.1 入口迁移 ✅

原 `pyproject.toml` 的 `first-agent` 指向的是占位函数（`first_agent:main`，只打印一行问候），
这是 v1 的已知缺陷。迁移**已完成**：

1. ✅ v2 CLI 可运行（`first_agent.main:main`，已支持 `chat --mode react`）。
2. ✅ 更新 `pyproject.toml`：

```toml
[project.scripts]
first-agent = "first_agent.main:main"
```

3. ✅ v1 在 `archive/handwritten_v1/` 中使用自己的 `pyproject.toml` 与独立 `.venv`。
4. ✅ v1 不依赖主线包，也不被 v2 导入。
5. ✅ v1 复现命令见 `archive/handwritten_v1/ARCHIVE.md`。

### 18.2 记忆迁移

`scripts/migrate_memory_v1.py`：

- 备份原 `memory.db` 到 `memory.db.v1.bak`。
- 读取旧表。
- 为每一行分配默认 `user_id`，由环境变量 `DEFAULT_USER_ID` 提供。
- **把 v1 中文 key/category 规范化为 v2 英文键值**（必做，否则迁移后无法被 `home_city` 等新键命中）。
- 写入新表。
- 原文件不删除，迁移后只读保留。
- 迁移脚本必须可重复执行且幂等。

key 映射表：

| v1 key | v2 key | 说明 |
|---|---|---|
| 居住地 / 用户城市 | `home_city` | |
| 用户姓名 / 姓名 | `user_name` | |
| 出行偏好 | `transport_mode` | |
| 饮食偏好 | `dietary_preference` | S5 用真实数据补入（R8） |
| 长期计划 | `long_term_plan` | |
| 其他未列出 key | 原样保留并打 `WARN` 日志 | 无法识别时不静默丢弃 |

category 映射表：

| v1 category | v2 category |
|---|---|
| 用户身份 | `user_identity` |
| 用户偏好 / 偏好 | `preference`（S5 用真实数据补入，见 R8） |
| 习惯 | `habit` |
| 长期计划 | `long_term_plan` |
| 其他未列出 category | `preference` + `WARN`（保守兜底） |

v2.0 不支持多值（如“饮食偏好=不吃辣，爱甜食”这类复合值）：迁移时保留原始字符串但按 `long_term_plan`/`preference` 归类，并在迁移报告中列出，供人工决定是否拆分。

### 18.3 兄弟目录关系

以下目录只作为参考，不合并：

- `AI1/`
- `knowledge-agent/`
- `travel-agent-langchain/`
- `travel-agent-langgraph/`
- `travel-agent-rag/`

v2 只建立在 `first-agent/`。

---

## 19. 里程碑

> 实现进度（截至本文档 v0.4）：**Phase 0 ✅ / Phase 1 🟡 / Phase 2 ✅ / Phase 3a ⏳ / Phase 3b ⏳ / Phase 4 🟡 / Phase 5 ⏳**
>
> 实际执行被拆成 S0～S8 若干小步，每步一个 commit + 一次全量测试。其中 **S2「护栏提前」** 把 Phase 4
> 的测试骨架（ScriptedLLM / Fake provider / runner / metrics / 回归门禁）提前实现，理由：没有护栏
> 就不该继续往深里写。这修正了原计划「评测放在最后」的内部矛盾。

| 阶段 | 状态 | 对应实现 |
|---|---|---|
| Phase 0 归档与入口保护 | ✅ | S0（tag `v1-handwritten` + `archive/handwritten_v1/`） |
| Phase 1 最小可运行 v2 | 🟡 | S1 契约层 + S3 ReAct；**README 骨架未做**，并入 Phase 5 |
| Phase 2 工具与动态记忆 | ✅ | S3 工具执行器 + S4 SessionStore / 动态注入 / 确定性 add-update-keep + S5 LLM 抽取与关系裁决 |
| Phase 3a Plan 结构与依赖 | ⏳ | S6a |
| Phase 3b 失败恢复 | ⏳ | S6b |
| Phase 4 评测与回归 | 🟡 | 护栏已提前到 S2；用例仅 5 条种子，`baseline.json` 与 CI 门禁待 S7 |
| Phase 5 文档与可选优化 | ⏳ | S8（含 README 重写） |

### Phase 0 ✅：归档与入口保护，0.5 天

- 打 tag `v1-handwritten`。
- 复制 v1 快照到 `archive/handwritten_v1/`。
- 归档**使用独立 venv**（保留原包名 `first_agent`，靠 `.venv` 隔离），避免与主线同名冲突。
- archive 内 `.gitignore` 排除 `.env`，复制时不带入任何 API Key。
- 确认 v1 独立可运行（已实测 `uv sync` + 模块导入 + `init_db()`）。
- 记录 v2 入口迁移计划。

退出条件：v1 在独立环境下可以按照 `archive/handwritten_v1/ARCHIVE.md` 启动，且仓库中不含 v1 的密钥。**已满足。**

### Phase 1 🟡：最小可运行 v2，1 天

- 新建 core、schemas、tools。
- 只实现天气工具和单轮 ReAct。
- 使用原生 Function Calling，并完成 Thought 归一化（`content → thought`，可空）。
- 建立 README 骨架（架构图与运行命令，后续阶段增量完善）。

退出条件：

```bash
uv run first-agent chat --mode react --user demo --session s1 "北京今天天气"
```

**已满足**（真实 API 冒烟通过）。唯一未做项是 README 骨架，顺延到 Phase 5。

### Phase 2 ✅：工具与动态记忆，1.5 天

- ToolRegistry、ToolResult、并行工具调用。
- SessionStore。
- 动态记忆注入。
- 修复 add/update/keep 映射。

退出条件：新记忆写入后，下一轮立即生效，并可通过测试。

**已满足**：`test_new_memory_is_injected_on_the_next_turn` 通过；另用真实 API 两轮验证了
历史回放（第二轮直接复用上一轮 Observation，未重复调用工具）。

### Phase 3a ⏳：Plan 结构与依赖，1.2 天

- PlanStep、`depends_on`、引用校验（三步）和拓扑排序。
- 多轮 `pending_slots`。

退出条件：三工具依赖用例可以按拓扑序执行并写入 Trace。

### Phase 3b ⏳：失败恢复，0.8 天

- retry、fallback 和一次 replan。
- 超预算熔断与可解释退出。

退出条件：工具失败/超时/依赖环用例均走结构化降级路径，不中断进程。

### Phase 4 🟡：评测与回归，1.5 天

- 30～40 条离线用例。
- ScriptedLLM 和 Fake Tools。
- baseline、current、diff。
- CI 回归门禁。

退出条件：无 API Key 可以一键跑完全部稳定指标。

**部分满足**：ScriptedLLM / Fake Tools / runner / metrics / 方向化门禁 / 端到端离线跑通均已完成（S2）；
剩余为把 `cases.jsonl` 从 5 条扩到 30～40 条、生成 `baseline.json`、加 CI 门禁（S7）。

### Phase 5 ⏳：文档与可选优化，0.5～1 天

- 完善 README 和架构图（骨架已在 Phase 1 建立）。
- CLI：chat、memory、trace、eval。
- 可选 FastAPI。
- 可选启发式 Router 和 route accuracy 评测。
- 更新简历项目描述。

总时间盒：7～8 天。

---

## 20. 最终叙事

v2 完成后可描述为：

> 从零实现 Tool Agent Runtime，保留 Thought 的 ReAct-style 循环使用原生 Function Calling 支持并行工具调用，并通过 PlanStep、依赖 DAG 和失败恢复实现多步骤编排；将记忆拆分为 Working、Short-term 和 Long-term 三层，通过确定性关系裁决、动态上下文注入和 SQLite session 隔离保证跨会话复用。构建 30～40 条离线评测和 baseline 回归，覆盖工具选择、参数抽取、多步规划、记忆冲突和工具降级。

不能写：

- 多 Agent 协作。
- RAG 知识库。
- 生产级 QPS。
- 真实线上用户规模。
- 没有 baseline 支撑的准确率提升。