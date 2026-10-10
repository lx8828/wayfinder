# Tool Agent Runtime（v2）

从零手写的 Tool Agent Runtime：**原生 Function Calling** 的 ReAct 循环 + **Plan-Execute（依赖 DAG）**、
三层记忆（Working / Short-term / Long-term）、会话隔离、耗时预算熔断与失败降级，
配 **36 条离线评测 + baseline 回归门禁**。

> 这是 `first-agent` 的 v2 主线。v1 手写实现已冻结在 Git tag `v1-handwritten`
> 与 [`archive/handwritten_v1/`](archive/handwritten_v1/ARCHIVE.md)（含独立环境，可单独运行）。
> 设计与决策记录见 [`docs/design.md`](docs/design.md)。

---

## 亮点速览

| 能力 | 实现方式 | 可验证证据 |
|---|---|---|
| ReAct 循环 | 原生 `tools + tool_calls`，保留 Thought（可空），支持并行多工具、`call_id` 配对 | `tests/test_react_engine.py` |
| 多步编排 | `PlanStep` + `depends_on` + `$steps.<id>.<field>` 引用，三步校验 + 拓扑排序 | `tests/test_plan_validation.py`、`test_plan_engine.py` |
| 长期记忆 | LLM **只判关系**（same/conflict/complementary/unsupported），`add/update/keep/skip` 由代码裁决 | `tests/test_memory_service.py` |
| 会话隔离 | `SessionStore` + 首轮重建上下文；**不用全局单例** | `tests/test_runtime_memory_hook.py` |
| 预算与降级 | 60s 硬熔断；工具失败重试、依赖失败阻塞下游、Plan 失败**降级到 ReAct**、非法计划**只重规划一次** | `tests/test_budget_recovery.py` |
| 契约化 | 每个工具**同时**声明输入与输出 Pydantic 模型，注册表统一生成 function-calling schema | `tests/test_tool_registry.py` |
| 评测与回归 | 36 条用例 + 离线硬门禁 + 真 LLM 段 + LLM-as-Judge | `first-agent eval` |

---

## 快速开始

```bash
uv sync
uv run pytest                # 397 个离线测试，不需要任何 API Key
uv run first-agent eval      # 36 条用例 + 离线硬门禁，不需要任何 API Key

cp .env.example .env         # 填 DEEPSEEK_API_KEY（天气需 QWEATHER_API_KEY）
uv run first-agent chat "明天下午去公园，天气和日程允许吗？" --mode plan
```

---

## 架构

```text
CLI / (可选) API
    │
    ▼
SessionResolver ── SessionStore          # user_id / session_id 解析与持久化
    │
    ▼
ContextBuilder ◀──(读)── 长期记忆          # 每轮重建，不用初始化时的快照
    │                              ▲
    ▼                              │(写)
ToolAgentRuntime（组合根）            │
    ├── ReActEngine ────────┐      │
    └── PlanExecuteEngine ──┴─▶ ToolRegistry ─▶ Tools ─▶ Providers（外部 IO）
                                    │
                                    ▼
                              Answer ─▶ MemoryService（抽取 → 裁决 → 落库）
                                    │
                                    ▼
                               TraceLogger（脱敏 JSONL）
```

- **`core/`**：基础设施（LLM 客户端与 `chat_json` 状态机、上下文、会话、预算、Trace、SQLite 封装）
- **`runtime/`**：ReAct 与 Plan-Execute 控制流
- **`tools/` / `providers/`**：工具定义与外部 IO 实现分离（`providers` 不放在 `tools` 下）
- **`memory/`**：长期记忆抽取、裁决、落库与 v1 迁移
- **`evals/`**：用例、离线/真 LLM 装配、编排与门禁、LLM-as-Judge

---

## 关键设计（每条都给出「为什么」）

1. **不自造 JSON Action 协议**。ReAct 直接使用原生 `tools + tool_calls`：参数抽取精度更高、
   `call_id` 天然对齐、不必自己制造并修复 JSON 解析失败面。Thought 来自模型**同一次响应**的
   `content`（可空，不为 Thought 多调一次 LLM）——因此准确表述是 *ReAct-style（Thought 可选）*。
2. **Plan 只有一套寻址**。`depends_on` 声明依赖，参数里用 `$steps.<id>.<field>` 取值；
   引用校验做三步（step 存在 / 在 `depends_on` 中 / field 在对方 `output_model` 里），
   执行顺序只由拓扑排序决定、与书写顺序无关。
3. **记忆里"值得存"与"怎么存"分开**。Extractor 只判「值不值得长期保存」；
   Adjudicator **只在 key 已存在**时判「与旧值什么关系」；`add / keep / update / skip`
   由确定性代码裁决。这样 v1 那个「LLM 说 add 但代码不处理、数据被静默丢弃」的 bug
   在结构上不可能重现。
4. **不用全局 Agent 单例**。v1 的 Web 端单个 `_agent` 全局共享 `messages`，
   多用户/多标签页互相串话且永不清理。v2 由 `SessionStore` 承载会话，
   system prompt 每轮用最新记忆重建。
5. **预算是硬熔断，不是提示词**。单次 run 累计耗时达 60s 即停止发起新调用，
   有观测就返回「部分结果 + 说明」，没有就给可解释失败，Trace 记 `budget_exceeded`。
   实测 `--budget-ms 1` 时 **LLM 调用数为 0**（一分钱不花）。
6. **失败必须走结构化降级路径**：工具瞬时错误重试一次；依赖失败的步骤标记
   `blocked_by_dependency` 并跳过下游而不是整体崩掉；Plan 生成失败或重规划后仍非法时
   **降级到 ReAct**，拿不到计划才退化为追问。

---

## 评测与回归门禁

### 两段式评测

| | 离线段 | 真 LLM 段 |
|---|---|---|
| 命令 | `first-agent eval` | `first-agent eval --real` |
| 模型 | `ScriptedLLM`（夹具） | `deepseek-chat` |
| 外部世界 | Fake provider + 固定基准日 | **同样的** Fake provider（只换模型） |
| 作用 | **Runtime 忠实度**（回归检测） | **模型能力**（只告警） |
| 门禁 | 硬门禁（稳定指标恶化 > 1pt → 失败退出） | 只告警（波动指标恶化 > 3pt） |

真 LLM 段刻意**只换模型、不换外部世界**——否则无法判断指标变化来自模型还是来自外部服务。

### 用例分布（36 条，由测试钉死）

| 类型 | 数量 | | 类型 | 数量 |
|---|---:|---|---|---:|
| 单工具调用 | 8 | | 工具失败与降级 | 4 |
| 双工具组合 | 6 | | 记忆 add/update/keep/skip/ignore | 6 |
| 三工具依赖链 | 5 | | 无关问题不调用工具 | 3 |
| 缺少参数追问 | 4 | | | |

另有 `judge_cases.jsonl` 5 条**开放式**用例走 LLM-as-Judge（`--real --judge`），
单独报告 `Task Success Rate`。

### 实测数字（本机一次运行）

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

**怎么读**：离线的 100% 是**期望值**（夹具即"称职模型"），它衡量 Runtime 是否忠实执行，
价值在回归检测；真 LLM 段才是模型能力。两段的差距（序列 100 → 80）正是本项目想量化的东西。

### 门禁有效性（变异测试，不是"跑一次好看"）

| 注入的缺陷 | 结果 |
|---|---|
| `resolve_arguments` 不解析 `$steps` 引用 | `Argument Accuracy` 恶化 → 门禁失败（exit 1） |
| 把 `conflict` 错映射为 `keep` | `Memory Decision Accuracy` 恶化 → 门禁失败（exit 1） |
| 去掉工具调用前的预算检查 | 3 条预算用例失败 |

产物落在 `src/first_agent/evals/`：`baseline.json`（离线基线）、`current.json`、`diff.md`
（真 LLM 段为 `*_live.*`），并随结果记录 **git commit / model / prompt version /
tool schema version / case set hash**，可回答「这次指标变化对应哪次改动」。
`--update-baseline` 会**拒绝**把失败结果写为基线。

---

## CLI 速查

| 命令 | 作用 |
|---|---|
| `first-agent chat "<问题>" [--mode react\|plan] [--user U] [--session S] [--show-thought] [--show-memory] [--budget-ms N]` | 单轮对话（默认 `react`） |
| `first-agent eval [--real] [--judge] [--update-baseline] [--no-gate] [--quiet] [--out DIR]` | 跑评测与回归门禁 |
| `first-agent memory list [--user U \| --all-users] [--json]` | 查看长期记忆 |
| `first-agent memory show <key> [--user U] [--json]` / `memory rm <key> [--user U]` | 查看 / 删除单条记忆 |
| `first-agent trace stats [--dir DIR] [--json]` / `trace tail [--n N]` | Trace 聚合统计 / 最近事件 |

`memory` / `trace` 只做读取与统计，数据层在 `core/inspect.py`（可直接单测），CLI 只负责打印。

---

## 隐私与 Trace

- Trace 写入 `var/traces/YYYY-MM-DD.jsonl`，默认 `TRACE_MODE=safe`：
  `user_id` / `session_id` 做 hash，手机号、邮箱掩码，`user_name` 一类敏感 key 的值整体掩码。
- 数据库中的 `user_id` 保留明文、Trace 中 hash，是**刻意**的不一致
  （DB 是本地文件，Trace 可能被外发）。
- **已知缺口**：`final_answer` 等自由文本只做手机号/邮箱掩码，用户自报的姓名可能原样落盘。
  因此 Trace 目录不进版本库，也不要把 `var/traces/` 外发；本地调试可用 `TRACE_MODE=full`。

---

## 已知限制 / 未做

- **自动 Router 未实现**：v2 由用户显式选择 `--mode`（设计里 A3 的取舍），不做自动路由判定。
- **无 HTTP 服务层**：只有 CLI。服务化（FastAPI）是可选阶段，未做——但 `SessionStore` 已按多会话设计，
  接入时不需要改内核。
- **单值记忆**：`UNIQUE(user_id, key)`，不支持多值 KV 与记忆历史版本；`complementary` 关系按 `skip` 处理并记 Trace。
- **LLM-as-Judge 的局限**：目前复用同一客户端（仅 prompt 独立），判定偏宽松，
  且 `Task Success Rate` 只统计裁判成功的样本。
- **`estimate_travel_time` 是启发式实现**（距离 ÷ 速度），不是实时路况——项目没有接实时交通 API，
  宁可给确定性实现并说明，也不假装有实时数据。
- **`cl100k_base` 未接入**：上下文预算用字符数近似 + 安全系数 1.2，避免首次联网下载词表破坏"离线可跑"。

---

## 质量保证

```bash
uv run pytest -q                  # 397 个测试，全部离线（ScriptedLLM + Fake provider）
uv run first-agent eval --quiet   # 离线评测门禁，无 API Key
```

- 每个里程碑一个 commit，改完跑一次全量测试 + 一次评测门禁。
- 关键保护逻辑都用**变异测试**验证过"测试真的会红"（预算熔断、`$steps` 解析、记忆裁决映射）。
- 所有外部 IO 都能注入 Fake：不联网、不需要 Key 也能覆盖失败/超时/空结果/降级路径。

---

## 简历描述（可直接取用）

> **Tool Agent Runtime（Python，从零实现）**：手写 Function Calling Agent 运行时，
> 支持 ReAct 与 Plan-Execute（依赖 DAG + 跨步骤引用）双模式，三层记忆与 SQLite 会话隔离，
> 耗时预算熔断和失败降级；建 36 条离线评测与 baseline 回归门禁，改代码后自动判定指标退化。

- 用原生 `tools + tool_calls` 替代自由 JSON Action，并以 `call_id` 对齐并行工具结果，
  避免自造协议带来的参数抽取精度损失。
- 记忆写入拆成「抽取 → 关系裁决 → 确定性落库」，其中 LLM 只判关系，
  从结构上消除了旧实现「LLM 判 add 但代码不处理导致数据丢失」的缺陷。
- 建立**两段式评测**（离线夹具硬门禁 + 真 LLM 波动指标）与方向化容差判定，
  并用变异测试证明门禁真能挡住回归；真 LLM 段量化出工具序列准确率 80.0%、平均延迟 1676.7ms。
- 397 个测试全部离线可跑（注入 ScriptedLLM 与 Fake provider），覆盖失败、超时、
  依赖阻塞、预算熔断与降级路径。

---

## 项目结构

```text
src/first_agent/
├── core/        # client / context / session / budget / tracing / db / inspect / engine
├── runtime/     # react.py（ReAct 循环）、plan_execute.py（DAG 编排）、slots.py
├── tools/       # registry.py、executor.py、builtin/（工具定义，不含网络实现）
├── providers/   # 真实与 Fake 外部 IO（天气 / 时间解析 / 日程 / 出行耗时）
├── memory/      # extractor / adjudicator / service / store / migrate
├── evals/       # cases.jsonl、offline.py、live.py、suite.py、judge.py、baseline.json
└── main.py      # CLI：chat / eval / memory / trace
tests/           # 397 个离线测试
archive/         # v1 归档快照（独立 venv，可单独运行）
docs/design.md   # 设计文档（含 R1–R18 实现偏差登记与里程碑）
```
