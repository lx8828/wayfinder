# Tool Agent Runtime 设计文档

- 项目：智能出行 Agent Runtime
- 仓库：`first-agent`（本文档位于 `docs/design.md`，随代码一同版本化）
- 版本：v0.4
- 日期：2026-10-10
- 状态：Proposed（S0–S14 已实现，见 0.1）
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
| R10 | 新增 `runtime/slots.py`（`SlotExtractor`） | design 8.2 要求「提取本轮槽位」，8.3 的例子也是按槽位合并；5 节目录未列此文件 | 5 / 8.3 |
| R11 | `ToolExecutor.execute(call, defaults=...)` 支持**会话级环境参数** | `get_calendar_events(user_id, ...)` 的 `user_id` 模型不可能知道；显式参数优先、defaults 兜底 | 11.2 / 8.2 |
| R12 | `estimate_travel_time` 用**启发式实现**（`HeuristicTravelProvider`，距离 ÷ 速度） | 项目没有接实时路况 API；宁可给确定性实现并说明，也不假装有实时数据 | 11.5 |
| R13 | 依赖失败的步骤**跳过**（`blocked_by_dependency`）而不是让整个 Plan 崩掉 | 一步失败就整体无回答，违背「失败可恢复」原则 | 8.3 |
| R14 | 预算由**门面**创建并贯穿整轮；熔断时补齐 `tool` 消息；降级成功时清空 `error`；预算耗尽跳过记忆写入 | 分别对应：引擎各自计时会漏算记忆开销 / 缺配对消息会让历史回放被真实 API 拒绝 / 成功的降级会被评测误判为失败 | 8.2 / 14.1 |
| R15 | 降级文案按「毫秒」如实显示小于 1 秒的预算 | `--budget-ms 1` 曾显示成「已达 1 秒」，与实际严重不符 | 14.1 |
| R16 | 运行时在**两种模式**下都注入 `user_id` 环境参数 | 此前只有 Plan 注入，导致 `get_calendar_events` 在 ReAct 模式**永远无法成功**（模型不可能知道 user_id） | 8.2 / 11.4 |
| R17 | 用例级新增「工具必须真的成功」判定（`tool_ok`） | 工具全挂但工具集合/参数恰好正确时，指标会假装一切正常（S7 首次运行就踩到：`invalid_output` 被 100% 的指标掩盖） | 16.2 |
| R18 | LLM-as-Judge 必须同时看到**工具观测数据** | 不给观测，裁判会把回答里的真实数值误判为「编造事实」（首次真实运行实测） | 16.3 |
| R19 | `TRACE_MODE` / `FIRST_AGENT_TRACE_DIR` 真正接入 CLI；新增 `core/inspect.py` 承载 `memory` / `trace` 的数据层 | 前两个环境变量文档写了但代码从未读取（文档与实现不符）；运维视图放在 CLI 里无法单测 | 15 / 5 |
| R21 | 新增 `--repeat N` 并落地 `Run-to-run Consistency`（结论 + 工具序列两个口径）；硬失败按「任意一轮失败」去重 | 设计 14.4 要求「真 LLM 跑 3 次观察稳定性」，但此前只能靠人工看日志；单轮数字无法区分「模型不稳」和「测试不稳」 | 14.4 / 16.5 |
| R26 | **规划前置校验**：工具输入字段声明 `slot_bindings`；步骤里出现"不在已知信息中的地点"或"我家/公司"这类相对说法 → 转 `need_info` 追问，**不执行任何工具**；既无步骤又无 `need_info` 的空计划计为结构缺陷 | 真跑中规划器会给缺失的槽位编一个值硬跑（不知道城市就填"北京"、拿"我家"当出发地），工具照样返回"正常"结果——**用户拿到的是编造的答案**。追问是唯一诚实的处理 | 8.2 |
| R27 | 复合意图：Planner Prompt 要求「和/并且/同时/顺便/、」分隔的每个需求都映射到至少一个工具步骤；另有**覆盖度启发式**只触发一次修复性重规划，重规划后仍不达标也照常执行 | `need_info` 的字段名是**结构化契约**，Prompt 必须给出规范词表（否则模型自造 `city`，`pending_slots` 与断言都对不上）；而覆盖度只是字数启发式，**不能用它否决一个结构合法的计划** | 8.2 |
| R28 | 工具方式契约：`mode` 是枚举（含 `train`），中英文别名（驾车/打车/高铁/driving…）在**工具契约层**归一化；provider 支持集必须与枚举一致（由一致性测试钉住） | "只在描述里列举取值"等于没要求：模型传 `driving` → provider 抛 `not_found`（像"查不到路线"，实为契约违例）。别名归一化让"说法不同"不再等于失败；枚举与 provider 不一致则会出现"合法方式反而查不到" | 11.5 |
| R24 | 断言按**证据层**分层（`observation` / `need_info` / `tool_error` / `final_answer`），并新增 `any_of` 与 `regex`；失败报告必须指明**是哪一条**断言失败 | 原实现把事实断言全写在回答文本上（要求中文回答出现英文槽位名、要求固定文案），业务正确但换说法的回答被判失败——**度量的是措辞而不是能力**。分层后同一事实按"工具返回的数据"判定，措辞交给 judge | 16.2 / 16.3 |
| R25 | 工具契约收紧：`estimate_travel_time.mode` 改枚举、`CalendarInput.date` 加 ISO 格式 `pattern` | 描述里写"walk/bike/…"等于没要求：真跑中模型传 `driving` → provider 抛 `not_found`（像"查不到路线"，实为契约违例）；日历收到非 ISO 日期 → 返回空列表 → 回答"你没有安排"，是**静默的错误答案**。收紧后变成显式 `invalid_arguments`，且 schema 会带上 `enum` / `pattern` 供模型遵循 | 11.5 / 16.2 |
| R23 | 顺序类指标拆成四个互补维度：`Tool Set Accuracy`（召回）/ `Precedence Accuracy`（关键先后）/ `Plan Validity Rate`（计划合法性四要素）/ `Plan Success Rate`（端到端）；用例新增 `precedence` 字段 | 只用「序列完全相等」无法归因：**合法换序**与**语义顺序错误**被记成同一类失败。真 LLM 实测证明这不是理论问题：`Tool Set` 91.67 而 `Tool Sequence` 66.67，约 25pt 的"序列错误"其实是合法换序 | 16.2 / 16.6 |
| R22 | CLI 启动时 `_configure_stdio()`：非交互输出切 UTF-8，交互控制台只放宽 `errors` | Windows 下 `first-agent eval > out.txt` / `\| Select-String` 会让 stdout 退化为 `gbk`，打印 `✅/✗/⚠` 直接 `UnicodeEncodeError`（exit 1、输出全空）；交互式控制台走 Windows 控制台 API 反而不受影响，所以只有重定向才暴露 | 15 / 19 |
| R20 | 补齐**摘要生成**（`memory/summarizer.py`）并明确原文窗口策略 | 10.1/10.3 的摘要一直是「契约 + 表 + 读取路径就绪、写入未接线」，实际行为是"只留最近 8 轮、旧的静默丢弃"，与 README「三层记忆」的表述不符（由用户发现）；同时修掉引擎窗口与构建器窗口**双重裁剪**（引擎取 16 轮、构建器又砍回 8 轮） | 10.1 / 10.3 |

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
│   ├── budget.py          # ✅ 耗时预算与超预算降级（S6b）
│   ├── context.py         # ✅ ContextBuilder（动态注入 + 字符预算）
│   ├── db.py              # ✅ 通用 SQLite 封装（R7，无业务 schema）
│   ├── session.py         # ✅ SessionStore / SessionResolver
│   ├── tracing.py         # ✅ TraceLogger 与脱敏
│   ├── inspect.py         # ✅ 只读运维视图（memory / trace 子命令的数据层，S8）
│   └── engine.py          # ✅ 组合根 ToolAgentRuntime（唯一跨层 import 点）
├── schemas/
│   ├── tool.py            # ✅ ToolSpec / ToolCall / ToolResult / NormalizedAssistantTurn
│   ├── plan.py            # ✅ Plan / PlanStep / $steps 引用解析
│   ├── memory.py          # ✅ MemoryCandidate / MemoryRelation / UserProfile
│   └── session.py         # ✅ WorkingState / SessionSummary
├── providers/
│   ├── base.py            # ✅ Provider 协议与异常体系（含 DateTime / Travel）
│   ├── fake.py            # ✅ Fake provider（失败注入 / 空结果；时间解析为规则式）
│   ├── weather.py         # ✅ QWeatherProvider（实况 + 3 天预报）
│   ├── datetime.py        # ✅ LlmDateTimeProvider（自然语言时间 → 具体日期）
│   ├── travel.py          # ✅ HeuristicTravelProvider（启发式耗时，无实时路况）
│   └── calendar.py        # ✅ LocalCalendarProvider（本地 JSON 日程）
├── tools/
│   ├── registry.py        # ✅ 显式注册表
│   ├── executor.py        # ✅ 校验 / 超时 / 重试 / 有界并发 / 环境默认参数
│   └── builtin/
│       ├── time.py        # ✅ get_current_time / resolve_datetime
│       ├── weather.py     # ✅ get_weather
│       ├── travel.py      # ✅ estimate_travel_time
│       └── calendar.py    # ✅ get_calendar_events
├── runtime/
│   ├── react.py           # ✅ ReActEngine
│   ├── plan_execute.py    # ✅ PlanExecuteEngine + 计划校验 / 拓扑排序
│   └── slots.py           # ✅ SlotExtractor（R10）
├── memory/
│   ├── store.py           # ✅ MemoryStore（确定性动作裁决）
│   ├── extractor.py       # ✅ 记忆抽取（LLM，别名规范化 + 逐条容错）
│   ├── adjudicator.py     # ✅ 关系裁决（LLM，失败保守跳过）
│   ├── service.py         # ✅ 三层编排（抽取 → 裁决 → 落库）
│   ├── summarizer.py      # ✅ 会话摘要（短期记忆，S9）
│   └── migrate.py         # ✅ v1 → v2 迁移（R9）
├── evals/
│   ├── scripted.py        # ✅ ScriptedLLM
│   ├── case.py            # ✅ EvalCase / 判定 / 加载（含 offline 夹具）
│   ├── runner.py          # ✅ RunOutcome / CaseCheck / 隔离执行
│   ├── metrics.py         # ✅ 指标与方向化 baseline 门禁
│   ├── adapters.py        # ✅ 真引擎 → RunOutcome（含 tool_errors / observations）
│   ├── offline.py         # ✅ 离线夹具装配（S7）
│   ├── live.py            # ✅ 真 LLM 装配（S7）
│   ├── suite.py           # ✅ 编排 / 元数据 / 门禁 / 产物（S7）
│   ├── judge.py           # ✅ LLM-as-Judge（S7）
│   ├── cases.jsonl        # ✅ 36 条用例
│   ├── judge_cases.jsonl  # ✅ 5 条开放式用例
│   ├── baseline.json      # ✅ 离线基线（硬门禁）
│   ├── current.json / diff.md        # ✅ 本次结果与对比
│   └── repeats_live.json  # ✅ 真 LLM 每轮明细与一致性（--repeat，S10）
└── main.py                # ✅ CLI（chat / eval / memory / trace）

tests/                     # ✅ 485 个离线测试（无 API Key 可跑）
var/                       # ✅ 本地 SQLite 与 Trace（已 gitignore）

.env.example               # ✅ 环境变量示例（不含密钥）
.github/workflows/ci.yml   # ✅ CI：pytest + 离线评测门禁（均不需要 API Key）
README.md                  # ✅ 用法、架构、评测读数与已知限制

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

实现补充（S6a）：

- `validate_plan` 在**执行前**完成上述三步 + 重复 id / 未知工具 / 依赖环 / 步数上限；
  执行期 `resolve_arguments` 再做一次引用解析，作为安全网。
- 执行顺序只由 `depends_on` 的拓扑排序决定，**与 steps 的书写顺序无关**
  （属性测试 `test_list_order_does_not_affect_the_result` 钉住这一点）。
- `ToolExecutor.execute(call, defaults=...)` 支持**会话级环境参数**（R11）：
  显式参数优先，`defaults` 兜底，`user_id` 这类模型不可能知道的参数由此注入。
- **依赖失败的步骤被跳过**（`blocked_by_dependency`）而非整体失败（R13）：
  其余步骤照常执行与汇总，失败原因通过 Observation 交给汇总环节。

实现补充（S6b，失败恢复）：

- **replan 只做一次**：校验失败 → 把「上一次计划 + 校验问题」回灌规划器再要一次；
  仍不合法即降级，不做第二次重规划。
- **降级到 ReAct**：Plan 生成失败（`chat_json` 校验不过）或重规划后仍不合法时，
  若注入了 `fallback_engine` 则转交 ReAct，并保留其答案 / 工具轨迹 / Thought
  （结果标记 `degraded_to="react"`）；未注入则退化为追问用户。
- 降级成功时**清空 `error`**：否则一次成功的降级会被评测判成失败。
- 工具层的 retry 仍由 `ToolExecutor` 负责（瞬时错误重试、非瞬时错误不重试），
  Plan 层不重复实现重试。

实现补充（S14，三条任务成功率改进）：

- **规划前置校验（R26，确定性）**：校验通过后、执行之前，检查每个工具的**必填**参数：
  若该参数声明了 `slot_bindings`（如 `get_weather.city → destination`），其取值必须是
  「本轮输入 + 已知用户信息 + 已填槽位」里出现过的值；`我家/公司/这里` 这类**相对说法**
  一律视为"槽位未补全"。命中即转 `need_info` 追问（回答里说中文槽位名，
  `need_info` 字段保留规范名），**一个工具都不执行**。
  - 只查**地点**：日期是可推导的（`resolve_datetime`/日期运算），把"日期没在输入里出现"
    当编造会误拦正确计划（真跑中确实误拦过），日期格式改由工具的 `pattern` 兜底。
  - 只查**必填**参数：有默认值的参数允许模型用默认值。
  - `need_info` 里的自造词（`city`/`目的地`/`出发地点`…）按 `SLOT_SYNONYMS` 归一化到
    规范槽位，保证 `WorkingState.pending_slots` 与评测断言拿到的是一套词汇。
- **空计划是缺陷**（R26 附带）：既没有步骤也没有 `need_info` 的计划计为 `empty_plan`
  结构问题 → 走一次重规划；否则会"零观测直接汇总"，产出静默的空洞回答（真跑中出现过）。
- **复合意图（R27）**：Prompt 增加第 8 条规则；另有 `detect_coverage_gap()` 用连接词数量
  估计需求数，**只用来触发一次修复性重规划**，重规划后仍不达标则照常执行
  （启发式不否决结构合法的计划，避免把一次正确的回答判成失败）。
- **出行方式（R28）**：`TravelMode` 枚举 + `MODE_ALIASES` 在契约层归一化；
  `MODE_SPEED_KMH` 必须与枚举一致（`test_tool_mode_enum_matches_provider_supported_modes`）。

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

实现状态：S4 落地「最近 N 轮窗口 + 字符预算裁剪」；**S9 补齐摘要生成**（`memory/summarizer.py`，见 10.3 的实现补充）。

窗口策略（S9 明确）：本轮保留**多少轮原文**由摘要覆盖情况决定——

```text
摘要覆盖 >= 总轮数 - RECENT_TURNS_KEEP - 1  → 保留 RECENT_TURNS_KEEP 轮（保底窗口 8）
否则（摘要缺失/未覆盖/失败）                 → 保留 min(总轮数, SUMMARY_TRIGGER_TURNS) 轮（最多 16）
```

第二种情况是**宁多勿丢**：摘要没跟上时若只留 8 轮，中间那些轮次既不在摘要里、也不在窗口里，
等于静默丢失历史。核心不变量为 ``摘要覆盖 + 最近窗口 = 全部轮次``（由
`tests/test_session_summary.py::test_summary_and_recent_window_cover_every_turn` 钉住）。

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

实现补充（S9）：

- 落在 `memory/summarizer.py`（`SessionSummarizer`），与 extractor / adjudicator 同层；
  阈值来自 `core.context`（`SUMMARY_TRIGGER_TURNS=16` / `SUMMARY_TRIGGER_TOKENS=16000`），
  并可由 `SUMMARY_TRIGGER_TURNS` / `SUMMARY_TRIGGER_TOKENS` 环境变量覆盖（10.1 要求配置化）。
- **只压缩**「已摘要范围之后、最近窗口之前」的轮次；`covered_through_turn` 推进到
  `总轮数 - keep - 1`，因此每次压缩都是增量的（新摘要会带上旧摘要作为输入）。
- 摘要输入只取用户输入与助手回答（不含 tool observation 原文），避免白占预算。
- 失败（`JSONValidationError` / `LLMError` / 空摘要）→ **不写库、不抛错**，保留旧摘要，
  Trace 记 `session_summary_failed`；门面另有 `_summarize` 兜底捕获任何异常。
- 后置执行：仅在预算未耗尽时调用（否则记 `session_summary_skipped_budget`），
  与本轮回答完全解耦。
- 真实实测（17 轮会话，deepseek-chat）：`covered_through=8`、`source_tokens=343` →
  `summary_tokens=105`（约 3.3× 压缩），摘要保住了偏好、工具结论与**未解决问题**。

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
| `resolve_datetime` | `expression, timezone` | `ResolvedTimeResult` | 处理“明天下午”；**结果含 `date` 字段供 `$steps.<id>.date` 引用** | ✅ S6a |
| `estimate_travel_time` | `origin, destination, mode` | `TravelTimeResult` | 多参数、可形成依赖；启发式实现（无实时路况） | ✅ S6a |
| `get_calendar_events` | `user_id, date` | `CalendarResult` | 本地 JSON 数据；`user_id` 由会话**环境默认值**注入 | ✅ S6a |

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

实现补充（S6b）：

- 预算由**门面**创建并贯穿整轮（引擎 + 后置记忆），引擎不再各自计时。
- 检查点固定在「发起新调用之前」：ReAct 为 `llm_call` / `tool_call`；
  Plan 为 `slot_extraction` / `plan` / `tool_call` / `summary`。
- 降级入口只有两处代码路径：`build_partial_answer()`（有 Observation 就列部分结果，
  没有就给可解释提示）与 Plan 的 `_fallback_summary()`（工具已跑完但没预算做汇总时，
  用确定性文本交付已有 Observation）。
- 门面在预算耗尽后**跳过记忆写入**，不再发起额外 LLM 调用。
- 熔断时**必须补齐 `tool` 消息**：否则被持久化的 `assistant(tool_calls)` 缺少配对响应，
  下一轮回放会被真实 API 拒绝（协议错误）。
- 文案如实报告耗时：小于 1 秒的预算按毫秒显示，不允许把 1ms 说成「1 秒」。

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

实现补充（S8）：

- `TRACE_MODE` 与 `FIRST_AGENT_TRACE_DIR` 已真正接入 CLI（R19，此前文档写了但代码未读取）。
- **已知缺口**：`final_answer` 等自由文本只做手机号/邮箱掩码，用户自报的姓名可能原样落盘。
  因此 `var/traces/` 不进版本库、不外发。要彻底避免只能不落答案文本，
  但那会让 Trace 失去排障价值——这是**有意的取舍**，写进 README 的隐私说明。

---

## 16. 评测设计

### 16.1 评测用例规模

首版 30～40 条：

| 类型 | 数量 | `category` |
|---|---:|---|
| 单工具调用 | 8 | `single_tool` |
| 双工具组合 | 6 | `double_tool` |
| 三工具依赖链 | 5 | `chain` |
| 缺少参数追问 | 4 | `need_info` |
| 工具失败与降级 | 4 | `failure` |
| 记忆 add/update/keep/skip/ignore | 6 | `memory` |
| 无关问题不调用工具 | 3 | `no_tool` |

实现补充（S7）：本表已落成 `cases.jsonl` 的 36 条用例，并由
`tests/test_evals_harness.py::test_case_set_matches_design_distribution` 断言钉住——
**改用例就必须同步改本表**，否则测试直接失败。

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
    {"type": "contains", "target": "observation", "value": "项目评审会"},
    {"type": "any_of", "values": ["评审", "会议", "安排"]}
  ]
}
```

**断言必须写在正确的证据层上**（S13 修订，见 R24）：

| `target` | 证据来自 | 用途 | 例子 |
|---|---|---|---|
| `observation` | 工具**实际返回的数据** | 关键实体 / 数值 / 日期。验证「这条事实有没有被交付给模型」，与模型怎么措辞无关 | `项目评审会`、`320`、`2026-10-11` |
| `need_info` | 引擎结构化输出的待补槽位 | 追问类用例断言槽位名，**不要求中文回答里出现英文 slot 名** | `destination` |
| `tool_error` | 失败调用的 `error_code` | 失败与降级路径 | `unavailable`、`timeout` |
| `final_answer` | 助手回答文本 | **只用于措辞稳定的确定性输出** | 工具直接给出的 `16:30`、`1+1=2` |

匹配方式：`contains` / `not_contains` / `equals` / `any_of`（同义表达任一命中）/ `regex`（形态稳定的模式，如日期）。
加载期即校验写法（`any_of` 必须有 `values`、`regex` 必须能编译、其余必须有 `value`）。

> **反面教材（S13 之前的写法）**：要求中文回答里出现英文槽位名 `origin`、要求回答包含固定文案
> `项目评审会`、要求固定的解释方式。结果「业务上正确但换了说法」的回答被判失败——
> **那是尺子太硬，不是项目差**。自由文本的表现力问题交给 16.3 的 LLM-as-Judge。
>
> 防回退：`tests/test_assertions.py::test_case_set_has_no_brittle_answer_only_fact_assertions`
> 要求「观测/槽位/错误层的断言数 ≥ 回答层字面断言的 **2 倍**」，改回写死措辞会让它失败。

三个工具字段不可混用，按 `mode` 决定以谁为准：

| 字段 | 适用 mode | 语义 |
|---|---|---|
| `expected_tool_sequence` | `plan` | 必须**按序**匹配，且序列须属于合法拓扑序 |
| `allowed_topological_orders` | `plan` | 可选；当存在多个合法拓扑序时，允许列表中的任一序 |
| `expected_tool_set` | `react` | 只比较**集合**，不约束顺序 |
| `expected_arguments` | 两者 | 期望参数（值可引用 `initial_memory`，如 `home_city → city`） |

- `plan` 用例若同时填写 `expected_tool_sequence` 与 `allowed_topological_orders`，**以 `allowed_topological_orders` 为准**；`expected_tool_sequence` 视为默认拓扑序（写入基线用）。
- `expected_status` 为可选字段，只表示运行状态，**不能代表答案正确**。

实现补充（S7）：

- **显式声明才判定**：`"expected_tool_set": []` / `"expected_tool_sequence": []` 表示
  **不允许调用任何工具**；字段缺省才是「不约束」。二者语义必须区分，否则「什么都没调」
  会被当成「没约束」而静默通过。
- `category`：用例类型，与 16.1 的表一一对应，仅用于统计与文档对照。
- `precedence`：**关键先后约束**，形如 `[["resolve_datetime", "get_weather"]]`，表示前者必须出现在后者之前。
  只声明语义上必要的顺序（如"日期要先解析出来"），**不追求唯一序列**——ReAct 的顺序本来就不唯一。
  加载期即校验「必须是两个不同工具」，避免畸形夹具在跑用例时才炸掉整批。

**顺序类指标拆成四个互补维度**（``Tool Sequence Accuracy`` 仍是主指标之一，但不再是唯一）：

| 指标 | 判定口径 | 回答的问题 |
|---|---|---|
| `Tool Sequence Accuracy` | 序列完全匹配（plan 可用 `allowed_topological_orders` 放宽） | 是否走了预期路径 |
| `Tool Selection Accuracy` | 集合**完全相等** | 有没有画蛇添足 |
| `Tool Set Accuracy` | 必要工具**全覆盖**（只召回，不惩罚多余） | 该做的做了吗 |
| `Precedence Accuracy` | 只检查声明的关键先后约束 | 依赖顺序对不对（不管无关顺序） |
| `Plan Validity Rate` | 集合正确 + DAG 合法 + 无失败工具 + 无多余调用 | 计划本身站得住吗 |
| `Plan Success Rate` | 计划合法 **且** 答案通过断言 | 端到端跑成了吗 |

这四类的意义在于**可归因**：只看 `Tool Sequence Accuracy` 时，「模型换了合法顺序」与
「模型先查天气再解析日期（语义错误）」都会被记成同一类失败。
- `expect_tool_errors`（默认 `false`）：默认**不允许任何工具执行出错**，只有「工具失败
  与降级」类用例置 `true`。这是独立于指标的用例级判定，用于堵死
  「工具全挂、但工具集合与参数恰好正确」造成的假绿（见 R17）。
- `offline`：离线夹具（ScriptedLLM + Fake provider 的确定输入），见 16.5。

### 16.3 评测口径

- Plan：比较有序 `Tool Sequence`（受上表规则约束）。
- ReAct：比较 `Tool Set`、参数和额外调用，不强制唯一顺序。
- 确定性答案使用断言。
- 开放式答案另建 `judge_cases.jsonl`，使用独立 LLM-as-Judge，并按 judge 准确率单独报告。

实现补充（S7）：

- `judge_cases.jsonl`（5 条）没有字符串断言，只有自然语言 `criteria`。
- 裁判输入 = 用户输入 + 验收标准 + **工具观测数据** + 助手回答。观测数据必不可少：
  不给观测时裁判会把回答里的真实数值误判为「编造事实」（R18，首次真实运行实测）。
- 裁判只回 `{"passed", "reason"}`；**裁判失败的样本不计入分母**（`judged=False`），
  否则指标会混入裁判自身的波动。
- `LlmJudge` 目前复用同一客户端 + 独立裁判 prompt；若追求更强独立性，可用
  `JUDGE_MODEL` 指定另一个模型（v2 未强制）。

### 16.4 数据库隔离

- 每个 case 使用独立临时 SQLite 或 `:memory:`。
- 用例结束后销毁 DB。
- 禁止在 cases 之间共享 memory.db。
- 测试并发执行时，每个 case 必须有唯一 session_id。

实现补充（S7）：由 `runner.run_cases_isolated(cases, factory)` 落地——**工厂模式**
（`case → 全新运行时`）而不是共享引擎，每个用例拿到独立的 `:memory:` DB；
用例专属 `user_id` 统一为 `case:<case_id>`（`case.eval_user_id()`），
时钟与基准日固定为 `2026-10-10`，保证相对时间可复现。

### 16.5 Baseline 回归

目录：

```text
evals/
├── baseline.json          # 离线基线（硬门禁，进 git）
├── current.json           # 本次结果（进 git）
├── diff.md                # 人类可读对比
├── baseline_live.json     # 真 LLM 基线（波动指标，只告警）
├── current_live.json
├── diff_live.md
└── repeats_live.json      # --repeat N 的每轮指标 / 波动 / 一致性 / 每轮裁判分数
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

实现补充（S7）：

- **两段式命令**：`first-agent eval`（离线：ScriptedLLM + Fake Tools，硬门禁）、
  `first-agent eval --real`（真模型 + **同样的 Fake 世界**，只告警）。真 LLM 段只换模型、
  不换外部世界——若连天气 API 一起换，就无法判断指标变化来自模型还是来自外部服务。
- **离线段的定位必须说清**：夹具显式声明「称职模型会怎么回答」，因此 100% 是期望值，
  它的价值在**回归检测**（已实测：破坏 `$steps` 解析 → `Argument Accuracy` 恶化 → 门禁失败；
  把 `conflict` 错映射为 `keep` → `Memory Decision Accuracy` 恶化 → 门禁失败）。
  模型能力由真 LLM 段衡量。
- 告警与门禁**都必须看容差**：否则 0.1pt 的抖动也会报警，几次之后就没人看了。
- 用例失败（含 `tool_ok`）与指标恶化同为硬失败；`--update-baseline` **拒绝**把失败结果写成基线。
- 元数据（commit / model / prompt_version / tool_schema_version / case_set_hash）随
  `baseline.json` 与 `current.json` 一起落盘，方便回答「这次变化对应哪次改动」。

实现补充（S9 复查，稳定性观测）：

- 新增 `--repeat N`：重复跑整套用例，输出**均值**与**波动（最大−最小）**，
  并把每轮指标 + 一致性 + 裁判分数写入 `repeats{suffix}.json`。
- `Run-to-run Consistency` 用两个口径：**结论**（`passed`）与**工具序列**（完全一致）；
  两者都给出，因为「结论偶尔翻转」和「工具顺序抖动」是两类不同问题。不稳定用例会被点名。
- 硬失败口径按「**任意一轮**失败」去重，避免重复跑把同一条用例的失败稀释掉。
- 离线段的波动应当恒为 0；一旦非 0 说明夹具里混入了非确定性（例如依赖真实时间）。

### 16.6 指标

| 指标 | 口径 |
|---|---|
| Tool Selection Accuracy | 选中工具（集合）**完全相等**的用例占比 |
| Tool Set Accuracy | **必要工具全覆盖**的用例占比（只看召回，多余调用由 `Extra Tool Call Rate` 负责） |
| Precedence Accuracy | 声明了 `precedence` 的用例中，**关键先后约束全部满足**的占比 |
| Plan Validity Rate | plan 用例中「集合正确 + DAG 合法 + 无失败工具 + 无多余调用」四项全满足的占比 |
| Plan Success Rate | plan 用例中「计划合法 **且** 答案通过断言」的占比（含答案 → 波动指标，只告警） |
| Tool Sequence Accuracy | Plan 用例中工具序列（含合法拓扑序）正确的占比 |
| Argument Accuracy | 期望参数键值全部命中的占比 |
| Structured Output Validity Rate | Plan / Memory 的 `chat_json` 一次或重试后校验通过率 |
| Extra Tool Call Rate | 超出 `expected_tool_set` 的多余调用占比 |
| Memory Decision Accuracy | 确定性用例中最终 DB 动作（add/update/keep/skip）正确的占比 |
| Cross-session Memory Recall | 跨会话可恢复的长期记忆占比 |
| Average Steps | 平均循环步数（越低越好） |
| Average Latency | 平均单次 run 耗时（越低越好） |
| Task Success Rate | **仅对 `judge_cases.jsonl` 的开放式用例**，按 LLM-as-Judge 判定正确的占比（只统计裁判成功的样本）；确定性用例不计入本指标（用 `assertions` 断言代替） |
| Assertion Pass Rate | 表外附加指标：声明了 `assertions` 的用例中全部断言通过的占比。S13 起断言分层（结构化 / 实体 / 关键约束），该指标衡量的是「事实与关键约束是否命中」，**不再惩罚措辞差异**；真 LLM 段仍波动，只作参考 |
| Run-to-run Consistency | 同一用例真 LLM 重复 N 次的**结论一致率**（`--repeat N` 实现；同时给出工具序列一致率）。实测 6 次运行：结论 80.6～88.9%、工具序列 72.2～77.8% |

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
| Plan 依赖可拓扑排序 | `tests/test_plan_validation.py::test_topological_order_is_dependency_first` | ✅ |
| 引用必须声明 depends_on | `tests/test_plan_validation.py::test_reference_requires_depends_on` | ✅ |
| 引用字段必须在 output_model 中 | `tests/test_plan_validation.py::test_reference_to_unknown_field_detected` | ✅ |
| 依赖环被拒绝 | `tests/test_plan_validation.py::test_cycle_detected` | ✅ |
| 步骤书写顺序不影响结果 | `tests/test_plan_engine.py::test_list_order_does_not_affect_the_result` | ✅ |
| 依赖值被解析进参数 | `tests/test_plan_engine.py::test_dependency_chain_resolves_reference_values` | ✅ |
| 多轮追问合并状态（Plan） | `tests/test_plan_engine.py::test_slots_merge_across_turns_without_resetting` | ✅ |
| need_info 写入 pending_slots | `tests/test_plan_engine.py::test_plan_mode_persists_pending_slots_through_runtime` | ✅ |
| 会话参数注入（user_id） | `tests/test_plan_engine.py::test_ambient_user_id_is_injected_into_tools` | ✅ |
| 依赖失败只阻塞下游 | `tests/test_plan_engine.py::test_failed_dependency_blocks_only_dependent_steps` | ✅ |
| 超预算可解释退出 | `tests/test_budget_recovery.py::test_run_budget_exceeded_stops_before_tool_and_explains` | ✅ |
| 熔断后不再发起新调用 | `tests/test_budget_recovery.py::test_plan_budget_exceeded_before_planning` | ✅ |
| 熔断保持消息协议完整 | `tests/test_budget_recovery.py::test_budget_break_keeps_message_protocol_valid` | ✅ |
| 有 Observation 时给出部分结果 | `tests/test_budget_recovery.py::test_budget_exceeded_returns_partial_results_when_available` | ✅ |
| 工具跑完但无预算汇总仍可交付 | `tests/test_budget_recovery.py::test_plan_budget_exceeded_at_summary_still_returns_observations` | ✅ |
| 预算耗尽后跳过记忆写入 | `tests/test_budget_recovery.py::test_runtime_skips_memory_when_budget_is_exhausted` | ✅ |
| Plan 只重规划一次 | `tests/test_budget_recovery.py::test_plan_gives_up_after_a_single_replan` | ✅ |
| Plan 失败降级到 ReAct | `tests/test_budget_recovery.py::test_invalid_plan_degrades_to_react` | ✅ |
| 降级成功不留失败痕迹 | `tests/test_budget_recovery.py::test_invalid_plan_degrades_to_react`（断言 `error is None`） | ✅ |
| 毫秒级预算如实显示 | `tests/test_budget_recovery.py::test_partial_answer_reports_milliseconds_for_sub_second_budget` | ✅ |
| 无 API Key 一键跑完全部稳定指标 | `tests/test_cli.py::test_eval_subcommand_runs_offline` | ✅ |
| 用例集与 16.1 分布一致（36 条） | `tests/test_evals_harness.py::test_case_set_matches_design_distribution` | ✅ |
| 每个用例独立 DB | `tests/test_eval_suite.py::test_each_case_gets_its_own_database` | ✅ |
| 指标恶化按方向判定并触发硬门禁 | `tests/test_eval_suite.py::test_gate_fails_on_stable_degradation_beyond_tolerance` | ✅ |
| 波动指标只在超过容差时告警 | `tests/test_eval_suite.py::test_volatile_metric_only_warns_when_beyond_tolerance` | ✅ |
| 工具执行失败即判用例失败 | `tests/test_eval_suite.py::test_tool_error_fails_the_case_even_when_expectations_match` | ✅ |
| `expect_tool_errors` 可声明允许失败 | `tests/test_eval_suite.py::test_expect_tool_errors_suppresses_the_guard` | ✅ |
| `$steps` 引用被真正解析 | `tests/test_plan_engine.py::test_case_plan_012_passes_through_harness` | ✅ |
| 裁判失败样本不计入成功率 | `tests/test_eval_suite.py::test_task_success_rate_excludes_unjudged_samples` | ✅ |
| 开放式用例端到端可判定 | `tests/test_eval_suite.py::test_run_judge_cases_end_to_end_with_a_fake_judge` | ✅ |
| 基线/结果/对比三件套落盘 | `tests/test_eval_suite.py::test_write_outputs_creates_current_and_diff` | ✅ |
| CLI 子命令齐备（无「尚未实现」） | `tests/test_cli.py::test_cli_surface_is_complete` | ✅ |
| `memory list/show/rm` 可用 | `tests/test_cli_inspect.py::test_memory_show_and_rm_roundtrip` | ✅ |
| `trace stats/tail` 可用 | `tests/test_cli_inspect.py::test_trace_stats_aggregates_runs_and_statuses` | ✅ |
| Trace 读取容忍坏行 | `tests/test_cli_inspect.py::test_read_trace_events_skips_malformed_lines` | ✅ |
| 摘要覆盖 + 最近窗口 = 全部轮次 | `tests/test_session_summary.py::test_summary_and_recent_window_cover_every_turn` | ✅ |
| 未到阈值不产生任何 LLM 调用 | `tests/test_session_summary.py::test_below_trigger_keeps_all_turns_without_any_llm_call` | ✅ |
| 摘要幂等（无新轮次不再调用） | `tests/test_session_summary.py::test_second_call_without_new_turns_is_idempotent` | ✅ |
| 增量摘要合并旧摘要、只覆盖新轮次 | `tests/test_session_summary.py::test_incremental_summary_merges_previous_and_only_covers_new_turns` | ✅ |
| 摘要失败保留旧摘要且不抛错 | `tests/test_session_summary.py::test_summary_failure_keeps_state_and_does_not_raise` | ✅ |
| 摘要失败不影响本轮回答 | `tests/test_session_summary.py::test_summary_failure_does_not_block_the_answer` | ✅ |
| 摘要注入上下文并按覆盖收窄窗口 | `tests/test_session_summary.py::test_window_narrows_when_summary_covers_earlier_turns` | ✅ |
| 摘要落后时窗口回退到 16 轮 | `tests/test_session_summary.py::test_window_falls_back_to_trigger_when_summary_is_missing` | ✅ |
| 预算耗尽跳过摘要 | `tests/test_session_summary.py::test_summary_is_skipped_when_budget_is_exhausted` | ✅ |
| 重复运行给出均值与波动 | `tests/test_eval_suite.py::test_repeat_runs_average_metrics_and_report_consistency` | ✅ |
| 跨轮不一致的用例被点名 | `tests/test_eval_suite.py::test_consistency_detects_flaky_cases` | ✅ |
| `Tool Set Accuracy` 只看召回、不惩罚多余 | `tests/test_plan_metrics.py::test_tool_set_accuracy_only_checks_recall` | ✅ |
| `Precedence Accuracy` 只约束声明的先后 | `tests/test_plan_metrics.py::test_precedence_ignores_unrelated_order` | ✅ |
| 先后约束颠倒/缺工具时判定失败 | `tests/test_plan_metrics.py::test_precedence_detects_reversed_order` | ✅ |
| 畸形 `precedence` 在加载期即报错 | `tests/test_plan_metrics.py::test_load_cases_rejects_malformed_precedence` | ✅ |
| `Plan Validity Rate` 四要素缺一不可 | `tests/test_plan_metrics.py::test_plan_validity_requires_all_four_conditions` | ✅ |
| 降级成功仍记「计划不合法」 | `tests/test_plan_metrics.py::test_degraded_run_is_not_counted_as_a_valid_plan` | ✅ |
| `Plan Success Rate` 要求合法且断言通过 | `tests/test_plan_metrics.py::test_plan_success_requires_validity_and_assertions` | ✅ |
| 只错顺序时召回不掉、先后掉 | `tests/test_plan_metrics.py::test_metrics_drop_when_precedence_is_violated_but_names_still_match` | ✅ |
| 事实断言读观测数据（不看措辞） | `tests/test_assertions.py::test_observation_layer_reads_tool_data_not_the_answer` | ✅ |
| 追问断言读结构化槽位 | `tests/test_assertions.py::test_need_info_layer_uses_structured_slots` | ✅ |
| 同义表达必须通过 | `tests/test_assertions.py::test_any_of_accepts_synonymous_phrasings` | ✅ |
| 日期可用多种格式通过 | `tests/test_assertions.py::test_regex_accepts_multiple_date_formats` | ✅ |
| 畸形断言在加载期报错 | `tests/test_assertions.py::test_load_cases_rejects_malformed_assertions` | ✅ |
| 用例集不得退回「写死措辞」 | `tests/test_assertions.py::test_case_set_has_no_brittle_answer_only_fact_assertions` | ✅ |
| 失败报告指出具体失败断言 | `tests/test_eval_suite.py::test_failure_report_names_the_failing_assertion` | ✅ |
| 出行方式必须落在枚举内 | `tests/test_builtin_tools.py::test_travel_tool_rejects_mode_outside_the_enum` | ✅ |
| 枚举写进工具 schema | `tests/test_builtin_tools.py::test_travel_tool_schema_declares_mode_enum` | ✅ |
| 非 ISO 日期被显式拒绝 | `tests/test_builtin_tools.py::test_calendar_tool_rejects_non_iso_date` | ✅ |
| 计划用了输入里没有的城市 → 追问不执行 | `tests/test_plan_readiness.py::test_plan_with_invented_city_asks_instead_of_executing` | ✅ |
| 「我家」这类相对地点 → 追问出发地 | `tests/test_plan_readiness.py::test_relative_place_is_treated_as_missing_slot` | ✅ |
| 来自记忆/槽位的值不算编造 | `tests/test_plan_readiness.py::test_value_from_memory_is_not_treated_as_invented` | ✅ |
| 可推导的日期不被误拦 | `tests/test_plan_readiness.py::test_derivable_date_is_not_treated_as_invented` | ✅ |
| 可选参数不参与缺槽位判定 | `tests/test_plan_readiness.py::test_optional_arguments_are_not_slot_checked` | ✅ |
| `need_info` 自造词归一化到规范槽位 | `tests/test_plan_readiness.py::test_need_info_synonyms_are_normalized_to_canonical_slots` | ✅ |
| 复合意图触发一次修复性重规划 | `tests/test_plan_readiness.py::test_composite_intent_triggers_one_repair_replan` | ✅ |
| 覆盖度启发式不否决合法计划 | `tests/test_plan_readiness.py::test_coverage_gap_never_blocks_a_structurally_valid_plan` | ✅ |
| 空计划先重规划、仍空则不算成功 | `tests/test_plan_readiness.py::test_empty_plan_is_repaired_by_replan` / `..._does_not_claim_success` | ✅ |
| 出行方式别名归一化（驾车/打车/高铁…） | `tests/test_builtin_tools.py::test_travel_tool_normalizes_common_mode_aliases` | ✅ |
| 无法识别的方式给出可选值 | `tests/test_builtin_tools.py::test_travel_tool_rejects_unknown_mode_with_legal_values` | ✅ |
| 工具枚举与 provider 支持集一致 | `tests/test_builtin_tools.py::test_tool_mode_enum_matches_provider_supported_modes` | ✅ |
| 非 UTF-8 重定向输出不崩（先证明不修必崩） | `tests/test_cli_encoding.py::test_configure_stdio_survives_non_utf8_stdout` | ✅ |
| 不支持 `reconfigure` 的流也不抛错 | `tests/test_cli_encoding.py::test_configure_stdio_handles_streams_without_reconfigure` | ✅ |
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

> 实现进度（截至本文档 v0.4）：**Phase 0 ✅ / Phase 1 🟡 / Phase 2 ✅ / Phase 3a ✅ / Phase 3b ✅ / Phase 4 ✅ / Phase 5 ✅**
>
> 实际执行被拆成 S0～S12 若干小步，每步一个 commit + 一次全量测试。**S9～S12 都是复查补漏**：
> S12 按「顺序类指标不该只有一个」把 `Tool Set / Precedence / Plan Validity / Plan Success`
> 拆出来（R23），真实数据证明拆分有效。
> S13 把断言从「回答措辞」重写到**证据层**并加失败断言诊断（R24），
> 顺带用新尺子暴露并修掉两个工具契约缺陷（R25）。
> S14 针对任务成功率做三件事：规划前置校验（缺槽位先追问，R26）、复合意图覆盖（R27）、
> 出行方式契约与别名（R28）。真实 3 轮：`Plan Success Rate` 23.81 → **42.86**、
> `Assertion Pass Rate` 58.89 → **71.11**、`Extra Tool Call Rate` 7.79 → **0.93**。
> 之前的**S9/S10/S11 也是复查补漏**：
> S9 发现 10.1/10.3 的摘要生成一直未接线（README 却写成「三层记忆」），故补齐实现并修掉窗口
> 双重裁剪（R20）；S10 落地 `--repeat` 与 `Run-to-run Consistency`（R21），真实跑 6 次填上
> 「设计要求跑 3 次但没有数字」的空缺；S11 修掉 Windows 重定向输出的编码崩溃（R22）
> 并同步 README 与本文件里所有过期表述（进度号、目录清单、测试数）。
>
> 教训：**文档里凡是能被机器核对的东西，都要么由测试守住、要么写保守值**——
> S9 的「三层记忆」、S11 的测试数/进度号，都是"实现变了文档没跟上"。
> 其中 **S2「护栏提前」** 把 Phase 4
> 的测试骨架（ScriptedLLM / Fake provider / runner / metrics / 回归门禁）提前实现，理由：没有护栏
> 就不该继续往深里写。这修正了原计划「评测放在最后」的内部矛盾。

| 阶段 | 状态 | 对应实现 |
|---|---|---|
| Phase 0 归档与入口保护 | ✅ | S0（tag `v1-handwritten` + `archive/handwritten_v1/`） |
| Phase 1 最小可运行 v2 | 🟡 | S1 契约层 + S3 ReAct；**README 骨架未做**，并入 Phase 5 |
| Phase 2 工具与动态记忆 | ✅ | S3 工具执行器 + S4 SessionStore / 动态注入 / 确定性 add-update-keep + S5 LLM 抽取与关系裁决 |
| Phase 3a Plan 结构与依赖 | ✅ | S6a |
| Phase 3b 失败恢复 | ✅ | S6b |
| Phase 4 评测与回归 | ✅ | S7：36 条用例 + 离线硬门禁 + 真 LLM 段 + LLM-as-Judge |
| Phase 5 文档与可选优化 | ✅ | S8（含 README 重写；Router / FastAPI 保留为可选，未做） |

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

### Phase 3a ✅：Plan 结构与依赖，1.2 天

- PlanStep、`depends_on`、引用校验（三步）和拓扑排序。
- 多轮 `pending_slots`。

退出条件：三工具依赖用例可以按拓扑序执行并写入 Trace。

**已满足**：`test_seed_case_plan_001_passes_through_harness` 通过；另用真实 API 跑通
「明天下午想去公园」，真实 DeepSeek 一次生成带 `$steps.0.date` 引用与正确 `depends_on`
的合法计划，且 `reason` 中明确引用了迁移来的 `home_city`。

### Phase 3b ✅：失败恢复，0.8 天

- retry、fallback 和一次 replan。
- 超预算熔断与可解释退出。

退出条件：工具失败/超时/依赖环用例均走结构化降级路径，不中断进程。

**已满足**：工具失败（`unavailable`）、依赖失败（`blocked_by_dependency`）、依赖环与
未知工具（重规划一次后 `invalid_plan`）均走结构化降级；60s 预算熔断有可解释退出。
另用真实 API 实测 `--budget-ms 1`：Trace 中 `run_budget_exceeded{stage:"llm_call"}`，
**LLM 调用数为 0**（一分钱没花），并跳过记忆写入，返回可解释降级回答、退出码 1。

### Phase 4 🟡：评测与回归，1.5 天

- 30～40 条离线用例。
- ScriptedLLM 和 Fake Tools。
- baseline、current、diff。
- CI 回归门禁。

退出条件：无 API Key 可以一键跑完全部稳定指标。

**已满足**：`cases.jsonl` 36 条（分布由测试钉死）、`judge_cases.jsonl` 5 条、
离线硬门禁（`baseline.json` + 方向化容差判定 + 用例级 `tool_ok`）、真 LLM 段（`--real`）、
LLM-as-Judge（`--judge`）、元数据落盘（commit / model / prompt / tool schema / case set hash）、
`first-agent eval` 一键跑完（无 API Key）。

**门禁有效性实测（变异测试）**：

| 注入的缺陷 | 结果 |
|---|---|
| `resolve_arguments` 不解析 `$steps` 引用 | `Argument Accuracy` 恶化 → 门禁失败（exit 1） |
| `RELATION_TO_ACTION["conflict"]` 改成 `keep` | `Memory Decision Accuracy` 恶化 → 门禁失败（exit 1） |
| ReAct 去掉工具调用前的预算检查 | 3 条预算用例失败（S6b） |

**真 LLM 段首跑（deepseek-chat，36 条用例 + 5 条 judge）**：

| 指标 | 离线（夹具） | 真 LLM |
|---|---:|---:|
| Tool Selection Accuracy | 100.00 | 95.24 |
| Tool Sequence Accuracy | 100.00 | 80.00 |
| Argument Accuracy | 100.00 | 94.74 |
| Memory Decision Accuracy | 100.00 | 80.56 |
| Extra Tool Call Rate | 0.00 | 4.88 |
| Average Steps | 1.72 | 1.58 |
| Average Latency | 0.6 ms | 1676.7 ms |
| Task Success Rate（judge） | — | 100.00（5/5 裁判成功） |

**读法**：离线段的 100% 是「期望值」（夹具即理想模型），它衡量 Runtime 忠实度，
价值在回归检测；真实段才是模型能力。两段差距（如 `Tool Sequence Accuracy`
100 → 80）正是 v2 想量化的东西，也是 v1 完全没有的数字。

**真 LLM 多次运行（S9 复查，`--real --repeat 3 --judge` × 2 批 = 6 次运行 / 216 次用例执行）**：

| 指标 | 批次 A 均值（波动） | 批次 B 均值（波动） |
|---|---:|---:|
| Tool Selection Accuracy | 92.07（4.76） | 98.41（4.76） |
| Tool Set Accuracy（召回） | — | 91.67（11.11） |
| Tool Sequence Accuracy | 71.11（**20.00**） | 64.45（6.67） |
| Precedence Accuracy | — | 66.67（**37.50**） |
| Plan Validity Rate | — | 61.90（14.29） |
| Plan Success Rate | — | 26.19（7.14） |
| Argument Accuracy | 89.47（0.00） | 92.98（5.27） |
| Memory Decision Accuracy | 81.48（2.77） | 80.56（8.33） |
| Extra Tool Call Rate | 5.28（8.33） | 6.02（2.50） |
| Assertion Pass Rate | 54.44（3.34） | 53.33（6.67） |
| Average Latency | 1729.6 ms（143.1） | 1764.4 ms（276.7） |
| Structured Output Validity Rate | 100.00（0.00） | 100.00（0.00） |
| Run-to-run Consistency（结论 / 工具序列） | 88.89% / 72.22% | 80.56% / 77.78% |
| Task Success Rate（judge，n=5） | 73.33（80/60/80） | 73.33（60/80/80） |

由此得到三条**可写进结论**的判断：

1. **`Structured Output Validity Rate` 6/6 轮 100%**：`chat_json` 状态机 + pydantic 校验
   在真实流量下没有一次崩掉——这是 v2 相对「裸 json.loads」最硬的收益。
2. **`Tool Sequence Accuracy` 是最大不稳定源**（单轮 60～80，同批摆动 20pt）：
   多步计划里模型会选择不同的合法分解与顺序，36 条用例中 13 条工具序列跨轮变化。
   引用该指标必须带波动，不能只报单轮。
3. **judge 自身是方差源**：同一用例在不同轮次出现「通过/不通过/通过」，
   5 条用例的样本量太小 → `Task Success Rate` 只能用多轮均值，单轮数字不可当结论。
4. **（R23 拆解后）`Tool Set` 91.67 明显高于 `Tool Sequence` 66.67**：
   模型「该调的工具有没有调」远好于「顺序是否完全一致」，约 25pt 的差额来自**合法换序**。
   而 `Precedence Accuracy` 66.67（同批摆动 **37.5pt**，最不稳）说明其中确有一部分是
   **语义顺序错误**（先查天气、后解析日期）。若只看 `Tool Sequence Accuracy`，
   这两类问题会被合并成一个数字，无法归因。
5. **`Plan Validity Rate` 61.90 → `Plan Success Rate` 26.19**：约 38% 的 plan 运行存在结构性缺陷
   （多余调用 / 工具失败 / 重规划后仍非法）；再叠加「答案通过断言」后只剩约 1/4。
   这个落差主要由**严格子串断言**造成（`Assertion Pass Rate` 53.33），
   而非答案不正确——开放式用例的 judge 通过率是 73.33。**两者必须一起读**。

6. **（S13 尺子修订）断言分层后的实测**：`Assertion Pass Rate` 从 53.3～54.4 升到 **58.9**（+5pt）。
   提升有限，且**已用失败断言诊断逐条核对**：残留失败全部是真实问题——
   模型没调工具（`plan_single_time_008`/`013`/`015` 零工具调用）、
   自己猜日期导致日历查空（`017` 未调 `resolve_datetime`）、
   该追问却不追问（`020`/`021`/`022` 未声明 `need_info`）、
   以及**契约给了可恢复错误却不重试**（`016`/`018` 收到 `invalid_arguments` 后未改 `mode`）。
   结论：这项指标低**主要是模型行为问题**，但原来的尺子无法区分「措辞不同」与「事实没到位」——
   现在失败报告会写明 `observation:contains(项目评审会)` 这类具体断言，可逐条归因。

7. **（S14 任务成功率）三条改进的净效果**（真实 3 轮，均值）：

   | 指标 | 改进前 | 改进后 |
   |---|---:|---:|
   | Plan Success Rate | 23.81 | **42.86**（±14.29） |
   | Assertion Pass Rate | 58.89 | **71.11**（±3.33） |
   | Plan Validity Rate | 59.52 | 66.67（±7.14） |
   | Tool Sequence Accuracy | 62.22 | 71.11 |
   | Tool Selection Accuracy | 90.48 | 98.41 |
   | Argument Accuracy | 87.72 | 92.98 |
   | Extra Tool Call Rate | 7.79 | **0.93** |
   | Precedence Accuracy | 54.17（±37.5） | 62.50（±0） |
   | Memory Decision Accuracy | 79.63 | 83.34 |
   | 工具序列一致率 | 69.44 | 88.89 |

   逐条归因（用失败诊断核对，不是"数字变好就行"）：
   - 已修好：`plan_chain_travel_016/018` 的 `invalid_arguments` / `not_found` **整轮归零**
     （方式别名生效）；`plan_need_info_city_020` 从"自造 `need_info` 词"变成正确追问；
     `plan_double_calendar_013` 从"零工具调用"变成真的查了日期与日程。
   - **我自己的新规则也误拦过一次**：日期按"是否出现在输入里"判定是错的（模型自己算出
     `2026-10-11` 属正确行为）→ 收窄为只查地点。这轮误拦是靠真实运行发现的，
     单测不会暴露——这也是"离线段是门禁、真 LLM 段是诊断"的价值。
   - 仍失败且属真实问题：模型对「现在几点？」这种无需信息的请求仍可能声明 `need_info`；
     少数用例的 `need_info` 用了词表外说法；旅行类轮次把出发地记成 `home_city`（记忆精度）。
   - `Plan Success Rate` 摆动 ±14.29，**读均值必须连摆动一起读**。

另外，真实段每轮有 17～20/36 条用例未通过。**必须分类归因，不能都算模型差**：
夹具/断言写死了理想模型的措辞（追问用例期望出现 slot 名 `destination`，真模型用中文回答）、
模型给出的合理不同分解、以及确有漏答；还出现过 `estimate_travel_time: not_found`
（模型传入的方式/城市超出 Fake provider 覆盖）。因此：**离线段是门禁，真 LLM 段是诊断**。

### Phase 5 ✅：文档与可选优化，0.5～1 天

- 完善 README 和架构图（骨架已在 Phase 1 建立）。
- CLI：chat、memory、trace、eval。
- 可选 FastAPI。
- 可选启发式 Router 和 route accuracy 评测。
- 更新简历项目描述。

**已满足**：README 重写（定位 / 用法 / 架构 / 评测读数 / 已知限制 / 简历描述）、
四个子命令齐备（`memory list|show|rm`、`trace stats|tail`）、`.env.example`、
CI 工作流（pytest + 离线门禁，均不需要 API Key）。

**有意不做**（保留为可选，并已写入 README 的「已知限制」）：

- **自动 Router**：A3 已判定 v2 由用户显式选模式；要做自动路由，得先有 route accuracy
  用例集，否则只是增加一个无法度量的分支。
- **FastAPI 服务层**：内核已按多会话设计（`SessionStore` + 每轮重建上下文），
  接入时不需改内核，因此不作为交付项。

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