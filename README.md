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
| 顺序类指标可归因 | 拆成四个互补维度：召回（Tool Set）/ 关键先后（Precedence）/ 计划合法性（Plan Validity）/ 端到端（Plan Success），不再只靠"序列完全相等" | `tests/test_plan_metrics.py` |
| 长期记忆 | LLM **只判关系**（same/conflict/complementary/unsupported），`add/update/keep/skip` 由代码裁决 | `tests/test_memory_service.py` |
| 会话隔离 | `SessionStore` + 首轮重建上下文；**不用全局单例** | `tests/test_runtime_memory_hook.py` |
| 短期记忆 | 超过 16 轮未摘要即滚动压缩成 `SessionSummary`（覆盖 + 最近窗口 = 全部轮次，不丢历史） | `tests/test_session_summary.py` |
| 预算与降级 | 60s 硬熔断；工具失败重试、依赖失败阻塞下游、Plan 失败**降级到 ReAct**、非法计划**只重规划一次** | `tests/test_budget_recovery.py` |
| 契约化 | 每个工具**同时**声明输入与输出 Pydantic 模型，注册表统一生成 function-calling schema | `tests/test_tool_registry.py` |
| 评测与回归 | 36 条用例 + 离线硬门禁 + 真 LLM 段 + LLM-as-Judge | `first-agent eval` |

---

## 快速开始

```bash
uv sync
uv run pytest                # 410+ 个离线测试，不需要任何 API Key
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
7. **短期记忆会滚动压缩**：未摘要轮次超过 16 轮（或估计 token 超过 16000）时，把「早于最近 8 轮」
   的对话压进 `SessionSummary`（幂等键 = `session_id + covered_through_turn`；失败保留旧摘要且不影响回答）。
   窗口策略保证 `摘要覆盖 + 最近窗口 = 全部轮次`；摘要没跟上时窗口自动放宽到 16 轮——
   宁可多带原文，也不要「既不在摘要、又不在窗口」的静默丢失。实测 17 轮会话压缩到 105 token
   （源 343），摘要保住了偏好、工具结论与未解决问题。

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

### 断言写在哪个证据层（S13 修订）

初版把事实断言全写在回答文本上——要求中文回答里出现英文槽位名 `origin`、要求包含固定文案
`项目评审会`。结果**业务上正确但换了说法的回答被判失败**：量的是措辞，不是能力。现在每条
断言都必须声明**证据层**（`target`）：

| `target` | 证据来自 | 用途 | 例子 |
|---|---|---|---|
| `observation` | 工具**实际返回的数据** | 关键实体 / 数值 / 日期 | `项目评审会`、`320`、`2026-10-11` |
| `need_info` | 引擎结构化给出的待补槽位 | 追问类用例 | `destination` |
| `tool_error` | 失败调用的 `error_code` | 失败与降级路径 | `unavailable` |
| `final_answer` | 助手回答文本 | **只用于确定性输出** | 工具直接给的 `16:30`、`1+1=2` |

匹配方式新增 `any_of`（同义表达任一命中）与 `regex`（日期等形态稳定的模式）；自由文本的
表现力交给 LLM-as-Judge。**失败报告会指出具体是哪一条断言失败**（如
`observation:contains(项目评审会)`），从而区分「模型错了」与「尺子太硬」。
防回退：`tests/test_assertions.py` 要求「结构化层断言数 ≥ 回答层字面断言的 2 倍」，
改回写死措辞会让测试失败。

**实测效果**：`Assertion Pass Rate` 由 53.3～54.4 升到 **58.9**；残留失败经逐条核对
**全部是真实问题**——模型没调工具、自己猜日期导致日历查空、该追问却不追问、
收到可恢复的 `invalid_arguments` 后不换合法参数重试。

### 用例分布（36 条，由测试钉死）

| 类型 | 数量 | | 类型 | 数量 |
|---|---:|---|---|---:|
| 单工具调用 | 8 | | 工具失败与降级 | 4 |
| 双工具组合 | 6 | | 记忆 add/update/keep/skip/ignore | 6 |
| 三工具依赖链 | 5 | | 无关问题不调用工具 | 3 |
| 缺少参数追问 | 4 | | | |

另有 `judge_cases.jsonl` 的 5 条开放式用例走 LLM-as-Judge（`--real --judge`），
单独报告 `Task Success Rate`。

### 实测数字

**离线（夹具）**：全部稳定指标 100.00 —— 这是**期望值**（夹具即"称职模型"），衡量 Runtime 是否忠实执行，价值在回归检测。

**真 LLM（deepseek-chat，`--repeat 3 --judge` 跑了 2 批，共 6 次运行 / 216 次用例执行）**：

| 指标 | 批次 A 均值（波动） | 批次 B 均值（波动） | 批次 C：断言分层后（波动） |
|---|---:|---:|---:|
| Tool Selection Accuracy（集合相等） | 92.07（4.76） | 98.41（4.76） | 90.48（9.53） |
| Tool Set Accuracy（召回） | — | 91.67（11.11） | 88.89（11.11） |
| Tool Sequence Accuracy（序列完全一致） | 71.11（20.00） | 64.45（6.67） | 62.22（33.33） |
| Precedence Accuracy（关键先后） | — | 66.67（**37.50**） | 54.17（37.50） |
| Plan Validity Rate（四要素） | — | 61.90（14.29） | 59.52（35.71） |
| Plan Success Rate（计划合法 + 断言通过） | — | 26.19（7.14） | 23.81（7.14） |
| Argument Accuracy | 89.47（0.00） | 92.98（5.27） | 87.72（10.53） |
| Memory Decision Accuracy | 81.48（2.77） | 80.56（8.33） | 79.63（2.78） |
| Extra Tool Call Rate | 5.28（8.33） | 6.02（2.50） | 7.79（7.50） |
| **Assertion Pass Rate** | 54.44（3.34） | 53.33（6.67） | **58.89（3.33）** |
| Average Steps | 1.51（0.12） | 1.54（0.11） | 1.51（0.14） |
| Average Latency | 1729.6 ms（143.1） | 1764.4 ms（276.7） | 1683.8 ms（92.3） |
| Structured Output Validity Rate | 100.00（0.00） | 100.00（0.00） | 100.00（0.00） |
| Run-to-run Consistency（结论） | 88.89% | 80.56% | 91.67% |
| Run-to-run Consistency（工具序列） | 72.22% | 77.78% | 69.44% |
| Task Success Rate（judge，n=5） | 73.33（80/60/80） | 73.33（60/80/80） | — |

> 批次 A/B 是断言修订**之前**的口径，批次 C 是**之后**（含工具契约收紧）。
> 三批之间的差异属正常波动（同批内摆动最大 37.5pt），**不要跨批比较单个数字**；
> 有意义的对比只有「同批内」与「口径明确标注的变化」（如 `Assertion Pass Rate` 的口径变化）。

「波动」= 同一批 3 轮的最大−最小；明细见 `src/first_agent/evals/repeats_live.json`。

**能得出的结论**：

- **稳定**：`Structured Output Validity Rate` 6/6 轮 100%——`chat_json` 状态机 + pydantic 校验
  在真实流量下没崩过；平均延迟 1.63～1.91s；`Memory Decision Accuracy` 80～81%。
- **最不稳**：`Precedence Accuracy` 同批摆动 **37.5pt**，`Tool Sequence Accuracy` 单轮 60～80
  （摆动 20pt）；36 条用例中 13 条的工具序列会跨轮变化。
- **拆分指标之后才能归因**：`Tool Set Accuracy` 91.67 明显高于 `Tool Sequence Accuracy` 66.67——
  模型「该调的工具有没有调」远好于「顺序完全一致」，约 25pt 的差额是**合法换序**；
  而 `Precedence Accuracy` 66.67 说明其余部分里确实有**语义顺序错误**（先查天气、后解析日期）。
  只看序列指标时，这两类问题会被合并成一个数字。
- **端到端要一起读两个数**：`Plan Validity Rate` 61.90（约 38% 的 plan 运行有结构性缺陷）→
  叠加「答案通过断言」后 `Plan Success Rate` 只剩 26.19。这个落差主要来自**严格子串断言**
  （`Assertion Pass Rate` 53.33），而不是答案不正确——开放式用例的 judge 通过率是 73.33。
- **要谨慎引用**：judge 只有 5 条用例太小，**同一用例的裁判结论会翻转**（实测出现
  通过/不通过/通过），单轮 `Task Success Rate` 不可当结论，只能用多轮均值。
- **约一半用例未通过**（每轮 17～20/36），原因分三类、不能都算模型差：夹具/断言写死了
  理想模型的措辞（如追问期望出现 slot 名 `destination`，真模型用中文回答）；模型给出合理的
  不同分解；以及确有漏答。**所以离线段是门禁（衡量 Runtime），真 LLM 段是诊断
  （衡量「模型 + 我的夹具假设」）。**

### 门禁有效性（变异测试，不是"跑一次好看"）

| 注入的缺陷 | 结果 |
|---|---|
| `resolve_arguments` 不解析 `$steps` 引用 | `Argument Accuracy` 恶化 → 门禁失败（exit 1） |
| 把 `conflict` 错映射为 `keep` | `Memory Decision Accuracy` 恶化 → 门禁失败（exit 1） |
| 去掉工具调用前的预算检查 | 3 条预算用例失败 |

产物落在 `src/first_agent/evals/`：`baseline.json`（离线基线）、`current.json`、`diff.md`
（真 LLM 段为 `*_live.*`），`--repeat N` 另写 `repeats{suffix}.json`（每轮指标、波动、
跨轮一致率、不稳定用例、每轮裁判分数）；并随结果记录 **git commit / model / prompt version /
tool schema version / case set hash**，可回答「这次指标变化对应哪次改动」。
`--update-baseline` 会**拒绝**把失败结果写为基线。

---

## CLI 速查

| 命令 | 作用 |
|---|---|
| `first-agent chat "<问题>" [--mode react\|plan] [--user U] [--session S] [--show-thought] [--show-memory] [--budget-ms N]` | 单轮对话（默认 `react`） |
| `first-agent eval [--real] [--judge] [--repeat N] [--update-baseline] [--no-gate] [--quiet] [--out DIR]` | 跑评测与回归门禁；`--real --repeat 3 --judge` 给真 LLM 的均值/波动与跨轮一致率 |
| `first-agent memory list [--user U \| --all-users] [--json]` | 查看长期记忆 |
| `first-agent memory show <key> [--user U] [--json]` / `memory rm <key> [--user U]` | 查看 / 删除单条记忆 |
| `first-agent trace stats [--dir DIR] [--json]` / `trace tail [--n N]` | Trace 聚合统计 / 最近事件 |

`memory` / `trace` 只做读取与统计，数据层在 `core/inspect.py`（可直接单测），CLI 只负责打印。

---

## Windows 注意（输出编码）

`first-agent eval > report.txt` 或 `first-agent eval | Select-String ...` 时，Python 的 stdout 会从
"Windows 控制台 API" 退化为 **ANSI 代码页**（简中为 `gbk`），此时打印 `✅ / ✗ / ⚠ / ✓`
会直接 `UnicodeEncodeError`：**命令 exit 1、输出文件全空**。CLI 启动时用 `_configure_stdio()` 处理：

- **重定向 / 管道输出** → 切成 UTF-8（与 `diff.md`、JSON 产物一致），重定向出来就是正确的 UTF-8 文件；
- **交互式控制台** → 保留控制台自身编码，只把 `errors` 设为 `replace`，中文照常显示，
  底层不支持的符号降级为 `?` 而不是抛异常；
- 想让控制台也按 UTF-8 显示（例如看清 `✅/✗`），可设 `PYTHONUTF8=1` 或先执行 `chcp 65001`。

回归测试：`tests/test_cli_encoding.py` —— 先用 `PYTHONIOENCODING=gbk` **证明"不修必崩"**，
再验证修复后的行为（这个"前提测试"保证该测试不会变成永远假装通过的空测试）。

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
- **LLM-as-Judge 的局限**：复用同一客户端（仅 prompt 独立），判定偏宽松；
  `Task Success Rate` 只统计裁判成功的样本；**5 条用例的样本量太小**，同一用例的裁判结论
  会跨轮翻转（实测），因此该指标只用多轮均值、不引用单轮数字。
- **端到端指标受断言口径影响**：`Plan Success Rate`（23.8）＝ 计划合法 + 断言通过。
  断言已按证据层重构（见「断言写在哪个证据层」），但计划类用例仍要求回答命中关键实体，
  **不能当作「模型只成功 1/4」来读**；端到端能力要与 judge 的 73.3 一起看。
- **模型不会从可恢复错误中恢复**：契约收紧后，非法参数会返回带**合法取值**的
  `invalid_arguments`（如 `mode` 必须是 walk/bike/bus/subway/car/taxi），
  但真跑中模型**不重试**、直接放弃该工具，导致该轮答案缺信息。这是模型行为问题，
  已被评测用例量化（`plan_chain_travel_016`/`018`）。
- **「契约写在描述里」是个坑**：日历工具原先只在字段描述里写「YYYY-MM-DD」，
  模型传非 ISO 日期 → provider 查不到 → 空列表 → 回答「你没有安排」，即**静默的错误答案**。
  现在格式写进 schema（`pattern`），非法输入变成显式错误。接真实外部 API 时应逐个排查同类问题。
- **夹具世界比模型的动作空间窄**：真跑中出现过 `estimate_travel_time: not_found`——
  模型传入的方式/城市不在 Fake provider 的覆盖范围里，暴露的是夹具覆盖面而非模型错误。
- **`estimate_travel_time` 是启发式实现**（距离 ÷ 速度），不是实时路况——项目没有接实时交通 API，
  宁可给确定性实现并说明，也不假装有实时数据。
- **`cl100k_base` 未接入**：上下文预算用字符数近似 + 安全系数 1.2，避免首次联网下载词表破坏"离线可跑"。

---

## 质量保证

```bash
uv run pytest -q                  # 410+ 个测试，全部离线（ScriptedLLM + Fake provider）
uv run first-agent eval --quiet   # 离线评测门禁，无 API Key
```

- 每个里程碑一个 commit，改完跑一次全量测试 + 一次评测门禁。
- 关键保护逻辑都用**变异测试**验证过"测试真的会红"（预算熔断、`$steps` 解析、记忆裁决映射）。
- 所有外部 IO 都能注入 Fake：不联网、不需要 Key 也能覆盖失败/超时/空结果/降级路径。
- Windows 重定向输出的编码崩溃由 `tests/test_cli_encoding.py` 钉住（含"不修必崩"的前提测试）；
  真 LLM 的稳定性由 `first-agent eval --real --repeat 3` 量化，不靠单次运气值。

---

## 简历描述（可直接取用）

> **Tool Agent Runtime（Python，从零实现）**：手写 Function Calling Agent 运行时，
> 支持 ReAct 与 Plan-Execute（依赖 DAG + 跨步骤引用）双模式，三层记忆与 SQLite 会话隔离，
> 耗时预算熔断和失败降级；建 36 条离线评测与 baseline 回归门禁，改代码后自动判定指标退化。

- 用原生 `tools + tool_calls` 替代自由 JSON Action，并以 `call_id` 对齐并行工具结果，
  避免自造协议带来的参数抽取精度损失。
- 把"顺序对不对"拆成四个互补指标（召回 / 关键先后 / 计划合法性 / 端到端）。
  实测证明这不是理论洁癖：`Tool Set` 91.67 而 `Tool Sequence` 66.67，
  约 25pt 的"序列错误"其实是合法换序，单指标会把两类问题混成一个数字。
- 把评测断言从「回答措辞」重写到「证据层」（工具观测 / 结构化槽位 / 错误码 / 确定性输出），
  并让失败报告指明具体断言；实测 `Assertion Pass Rate` 53.3 → 58.9，
  残留失败经逐条归因**全部是模型行为问题**——说明这项指标此前度量的是措辞而非能力。
  该重构还顺带暴露两个工具契约缺陷（自由字符串 `mode`、无格式校验的日期），已收紧为
  枚举与 `pattern`，把「静默的错误答案」变成显式错误。
- 记忆写入拆成「抽取 → 关系裁决 → 确定性落库」，其中 LLM 只判关系，
  从结构上消除了旧实现「LLM 判 add 但代码不处理导致数据丢失」的缺陷。
- 建立**两段式评测**（离线夹具硬门禁 + 真 LLM 波动指标）与方向化容差判定，
  并用变异测试证明门禁真能挡住回归；真 LLM 段用 `--repeat 3` 量化出模型能力**及其波动**：
  工具选择 92～98%、工具序列 64～71%（同批摆动最大 20pt）、平均延迟 1.73～1.76s、
  跨轮结论一致率 80.6～88.9%。
- 410+ 个测试全部离线可跑（注入 ScriptedLLM 与 Fake provider），覆盖失败、超时、
  依赖阻塞、预算熔断与降级路径。

---

## 项目结构

```text
src/first_agent/
├── core/        # client / context / session / budget / tracing / db / inspect / engine
├── runtime/     # react.py（ReAct 循环）、plan_execute.py（DAG 编排）、slots.py
├── tools/       # registry.py、executor.py、builtin/（工具定义，不含网络实现）
├── providers/   # 真实与 Fake 外部 IO（天气 / 时间解析 / 日程 / 出行耗时）
├── memory/      # extractor / adjudicator / service / summarizer / store / migrate
├── evals/       # cases.jsonl、judge_cases.jsonl、offline/live/suite/judge.py、baseline*/current*/diff*/repeats_live.json
└── main.py      # CLI：chat / eval / memory / trace
tests/           # 410+ 个离线测试
archive/         # v1 归档快照（独立 venv，可单独运行）
docs/design.md   # 设计文档（含 R1–R18 实现偏差登记与里程碑）
```
