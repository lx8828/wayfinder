# first-agent：一个手写的「天气出行助手」（Agent 运行时从零实现）

`first-agent` 是一个**天气出行助手**：问它天气、日程、两地耗时、当前时间，它会自己决定该调哪个工具、
多步的工具按依赖顺序调、缺信息就先追问，答完把值得记住的偏好写进长期记忆。

它跟一般"调 API 拼问答"的助手不同之处不在功能，而在**它的 Agent 运行时是从零手写的**：
没有用 LangChain / LlamaIndex，调用控制流、记忆、失败恢复、评测门禁全部自己实现。
因此每一层行为都能解释、能测试、能归因——出问题时能说清是**模型**的问题、
**运行时**的问题，还是**我的度量方式**有问题。

## 它是什么 / 能回答什么

真实用法（`first-agent chat "..."`）：

| 你说 | 它做什么 |
|---|---|
| 重庆明天天气怎么样？ | 调 `get_weather`，给出城市、日期、天气与提示 |
| 从重庆开车到成都大概要多久？ | 调 `estimate_travel_time`（出发地 + 目的地 + 出行方式） |
| 现在几点了？ | 调 `get_current_time` |
| 我明天下午有什么安排？ | 先 `resolve_datetime` 把"明天下午"解析成具体日期，再 `get_calendar_events`——**两步依赖** |
| 明天下午去公园，天气和日程允许吗？ | 解析日期 → 查天气 + 查日程（**依赖链 + 并行分支**）→ 综合给结论 |
| 我搬到杭州了，那边明天天气怎么样？ | 更新长期记忆里的常住城市，并用**新城市**查询 |
| 明天天气怎么样？（没说城市、也没有已知居住地） | **不猜城市**，先追问 |

### 一次真实的运行（Trace 实录）

```bash
first-agent chat --mode plan --show-thought "我住在重庆，明天下午想去公园，天气和日程允许吗？"
```

它自己生成的计划是三步，后两步都引用第一步的产出（`$steps.0.date`）：

```text
step 0  resolve_datetime    {"expression": "明天下午"}                              depends_on=[]
step 1  get_weather         {"city": "重庆", "date": "$steps.0.date"}                depends_on=[0]
step 2  get_calendar_events {"user_id": "2744ccd10c7533bd", "date": "$steps.0.date"} depends_on=[0]
```

三步全部成功（`status=success`、`steps=3`、`budget_spent_ms=6880`），回答是：

```text
可以，明天下午天气和日程都适合去公园。
关键信息：明天是 2026 年 10 月 11 日，重庆多云，气温 21 到 28 摄氏度，湿度 83%，
体感稍闷，建议穿轻薄透气衣物并带水；当天没有日程冲突，下午时间可自由安排。
```

这一次 6 秒的运行里就包含了大半关注点：**多参数工具、依赖 DAG、跨步骤引用、
运行时注入 `user_id`**（Trace 里它已被 hash —— `TRACE_MODE=safe`）、
**综合多源事实给结论**，以及**顺手更新长期记忆**（`memory_actions: home_city=add`）。

五个工具（定义在 `tools/builtin/`，外部 IO 在 `providers/`，全部可换 Fake）：

| 工具 | 作用 | 它在评测里负责什么 |
|---|---|---|
| `get_weather` | 城市天气 | 最常用的单工具场景 |
| `estimate_travel_time` | 两地耗时（**高德真实路径规划**，未配 Key 时用启发式估算） | **双参数**，能与日期/天气组成依赖链 |
| `resolve_datetime` | "明天下午" → `2026-10-11` / `14:00` | 产生**跨步骤引用**（`$steps.0.date`）的关键 |
| `get_calendar_events` | 某一天的日程 | 依赖运行时注入 `user_id`（模型不可能知道） |
| `get_current_time` | 当前时间 | 纯确定性输出，用来验证"不该编造" |

> 选这个场景不是为了做产品，而是它天然包含这些**运行时难题**：
> 多步依赖、参数由前一步产出、需要跨会话记忆、外部服务会失败、缺信息时不能瞎猜。
> 换句话说：**场景是为了把运行时逼到墙角而选的。**

## 运行时能力（手写的部分）

| 能力 | 一句话说明 |
|---|---|
| 两种控制流 | **ReAct**（边想边调工具）与 **Plan-Execute**（先出依赖 DAG 再执行），由 `--mode` 显式选择 |
| 三层记忆 | Working State（槽位）/ 短期（滚动摘要）/ 长期（SQLite KV）；LLM 只判关系，落库由确定性代码裁决 |
| 会话隔离 | 每个 `user_id + session_id` 独立上下文，每轮用最新记忆重建，不用全局单例 |
| 失败可恢复 | 工具瞬时错误重试、依赖失败只阻塞下游、Plan 失败**降级到 ReAct**、非法计划**只重规划一次** |
| 缺信息不猜 | 计划里出现"已知信息中没有的地点"或「我家」这类相对说法 → **先追问，不执行工具** |
| 硬预算熔断 | 单次 run 累计耗时超预算即停止发起新调用，返回「部分结果 + 说明」 |
| 评测即门禁 | 36 条离线用例（硬门禁）+ 15 条开放式用例（LLM-as-Judge），改代码后自动判定指标退化 |

---

## 30 秒跑起来

```bash
uv sync
uv run pytest                # 521 个离线测试，不需要任何 API Key
uv run first-agent eval      # 36 条用例 + 离线回归门禁，同样不需要 Key
```

需要真实模型时：

```bash
cp .env.example .env         # 填 DEEPSEEK_API_KEY（天气 / 出行耗时可选，见 .env.example）
uv run first-agent chat "重庆明天天气怎么样？" --mode react
```

真实输出（deepseek-chat）：

```text
重庆明天（2026-10-11）多云，气温 21 到 28 摄氏度，湿度 83%。
关键提示：温度较为舒适，适合外出，不过湿度偏高，体感可能略闷，建议穿着透气衣物。
```

Plan 模式下可以看它怎么规划与调用工具：

```bash
uv run first-agent chat --mode plan --show-thought "从重庆开车到成都大概要多久？"
```

要求：Python ≥ 3.13、[uv](https://docs.astral.sh/uv/)。离线测试与离线评测**零外部依赖**。

---

## 架构

```text
CLI
    │
    ▼
SessionResolver ── SessionStore          # user_id / session_id 解析与持久化（SQLite）
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

- **`core/`**：基础设施——LLM 客户端（含 `chat_json` 解析→校验→错误回灌→重试状态机）、
  上下文构建、会话、耗时预算、Trace、SQLite 封装、CLI 读数的数据层
- **`runtime/`**：两条控制流（`react.py` / `plan_execute.py`）与槽位抽取
- **`tools/` / `providers/`**：工具契约（输入输出模型 + 执行策略）与外部 IO **分离**——
  工具定义不碰网络，provider 才碰，因此所有外部 IO 都能换成 Fake
- **`memory/`**：长期记忆抽取、关系裁决、写入服务、滚动摘要、存储
- **`evals/`**：用例、离线/真 LLM 装配、编排与门禁、LLM-as-Judge

---

## 关键设计决策

每条都给出「为什么」，因为**知道为什么才能判断改动是否安全**。

1. **不自造 JSON Action 协议**：直接使用原生 `tools + tool_calls`。
   参数抽取精度更高、`call_id` 天然对齐并行结果、不必自己制造并修复一整套 JSON 解析失败面。
   Thought 取自模型**同一次响应**的 `content`（可空，不为它多调一次 LLM）——
   所以准确说法是 *ReAct-style（Thought 可选）*。
2. **Plan 只有一套寻址**：`depends_on` 声明依赖，参数用 `$steps.<id>.<field>` 取值。
   引用校验做三步（步骤存在 / 在 `depends_on` 里 / 字段在对方 `output_model` 里）；
   执行顺序由拓扑排序决定，与书写顺序无关。
3. **记忆里"值不值得存"与"怎么存"分开**：Extractor 只判"值不值得长期保存"；
   Adjudicator **只在 key 已存在**时判"与旧值是什么关系"；`add / keep / update / skip`
   由确定性代码裁决。这样"LLM 判了 `add` 但代码没处理、数据被静默丢弃"这类 bug
   在结构上不可能出现——**LLM 的输出不直接决定写库**。
4. **不用全局 Agent 单例**：把会话状态塞进模块级全局变量，会让多用户 / 多标签页互相串话，
   而且永远不清理。这里由 `SessionStore` 承载会话，system prompt 每轮用最新记忆重建。
5. **预算是硬熔断，不是提示词**：单次 run 累计耗时达到上限即停止发起新调用；
   有观测就返回「部分结果 + 说明」，没有就给可解释失败，Trace 记 `budget_exceeded`。
   实测 `--budget-ms 1` 时**该轮 LLM 调用数为 0**（一分钱没花）。
6. **失败必须走结构化降级路径**：工具瞬时错误重试一次；依赖失败的步骤标记
   `blocked_by_dependency` 并只跳过下游，而不是整体崩掉；Plan 生成失败或重规划后仍非法时
   **降级到 ReAct**，连 ReAct 也拿不到才退化为追问用户。
7. **短期记忆滚动压缩，且不丢历史**：未摘要轮次超过 16 轮（或估算 token 超 16000）时，
   把「早于最近 8 轮」的对话压进 `SessionSummary`（幂等键 = `session_id + covered_through_turn`；
   摘要失败保留旧摘要、不影响回答）。窗口策略保证「摘要覆盖 + 最近窗口 = 全部轮次」；
   摘要没跟上时窗口自动放宽——宁可多带原文，也不要「既不在摘要、又不在窗口」的静默丢失。
8. **缺信息时追问，而不是猜一个值把计划跑起来**：工具输入字段可以声明它对应哪个槽位
   （`get_weather.city → destination`）。执行前校验：必填参数的取值必须是
   「本轮输入 + 已知用户信息 + 已填槽位」里出现过的；`我家 / 公司` 这类相对说法一律视为未补全。
   命中就转成追问，**一个工具都不执行**——拿编造的参数去打外部服务，用户拿到的是编造的答案。
9. **约束写进 schema，而不是写进描述**：`mode` 是枚举（不是自由字符串）、日期带 `pattern`。
   只写在描述里的要求等于没要求：模型会传 `driving` 或"今天"，provider 报一个含糊的
   `not_found`，甚至返回空列表让回答变成「你没有安排」——那是**静默的错误答案**。

---

## 评测：为什么"跑一次好看"不算数

### 两段式评测

| | 离线段 | 真 LLM 段 |
|---|---|---|
| 命令 | `first-agent eval` | `first-agent eval --real [--judge]` |
| 模型 | `ScriptedLLM`（脚本夹具） | `deepseek-chat` |
| 外部世界 | Fake provider + 固定基准日 | **同样的** Fake provider |
| 回答什么 | **Runtime 忠实度**（回归检测） | **模型能力 + 波动**（只告警） |
| 门禁 | 硬门禁：稳定指标恶化 > 1pt 即失败退出 | 只告警：波动指标超容差 |

真 LLM 段刻意**只换模型、不换外部世界**——否则无法判断指标变化来自模型还是来自外部服务。

### 断言写在哪个证据层

事实断言如果写在**回答文本**上，量的就是措辞而不是能力：要求中文回答里出现英文槽位名
`origin`、要求包含固定文案 `项目评审会`，会让"业务正确但换了说法"的回答被判失败。
所以每条断言都要声明证据层（`target`）：

| `target` | 证据来自 | 用途 | 例子 |
|---|---|---|---|
| `observation` | 工具**实际返回的数据** | 关键实体 / 数值 / 日期 | `项目评审会`、`320`、`2026-10-11` |
| `need_info` | 引擎结构化给出的待补槽位 | 追问类用例 | `destination` |
| `tool_error` | 失败调用的 `error_code` | 失败与降级路径 | `unavailable` |
| `final_answer` | 回答文本 | **只用于确定性输出** | 工具给的 `16:30`、`1+1=2` |

匹配方式有 `contains` / `not_contains` / `equals` / `any_of`（同义表达任一命中）/
`regex`（日期等形态稳定的模式）；自由文本的表现力交给 LLM-as-Judge。
**失败报告会指明是哪一条断言失败**（如 `observation:contains(项目评审会)`），
从而区分「模型做错了」和「尺子太硬」。防回退由 `tests/test_assertions.py` 钉住：
它要求「结构化层断言数 ≥ 回答层字面断言的 2 倍」，改回写死措辞会让测试直接失败。

### 用例分布（36 条，由测试钉死）

| 类型 | 数量 | | 类型 | 数量 |
|---|---:|---|---|---:|
| 单工具调用 | 8 | | 工具失败与降级 | 4 |
| 双工具组合 | 6 | | 记忆 add/update/keep/skip/ignore | 6 |
| 三工具依赖链 | 5 | | 无关问题不调用工具 | 3 |
| 缺少参数追问 | 4 | | | |

另有 `judge_cases.jsonl` 的 15 条开放式用例走 LLM-as-Judge（`--real --judge`），
单独报告 `Task Success Rate`——它们没有字符串断言，只有自然语言验收标准，
覆盖记忆使用、失败诚实性、能力边界、多城市对比等"需要综合判断"的场景。
**单条用例的权重是 `100/N`**：15 条时一条只值 6.7pt，样本量由测试钉住
（`test_judge_case_set_is_large_enough_for_stable_rates`）。

### 当前实测数字

**离线段**：全部稳定指标 100.00 —— 这是**期望值**（夹具就是"一个称职的模型"），
它衡量的是 Runtime 有没有忠实执行，价值在回归检测，不代表真实能力。

**真 LLM 段**（deepseek-chat，3 轮 × 36 条 = 108 次用例执行；「波动」= 同批 3 轮的最大−最小）：

| 指标 | 均值（波动） |
|---|---:|
| Tool Selection Accuracy（工具集合完全相等） | 98.41（4.76） |
| Tool Set Accuracy（必要工具召回） | 90.74（2.78） |
| Tool Sequence Accuracy（序列完全一致） | 71.11（6.66） |
| Precedence Accuracy（关键先后约束） | 62.50（0.00） |
| Plan Validity Rate（计划四要素齐全） | 66.67（7.14） |
| Plan Success Rate（计划合法 + 答案通过断言） | 42.86（14.29） |
| Argument Accuracy | 92.98（5.27） |
| Memory Decision Accuracy | 83.34（8.33） |
| Extra Tool Call Rate（多余调用） | 0.93（2.78） |
| Assertion Pass Rate | 71.11（3.33） |
| Average Steps | 1.46（0.06） |
| Average Latency | 1659.2 ms（29.5） |
| Structured Output Validity Rate | **100.00（0.00）** |
| Run-to-run Consistency（结论 / 工具序列） | 86.11% / 88.89% |
| Task Success Rate（15 条开放式用例，3 轮） | 82.22（93.33 / 73.33 / 80.00） |

怎么读这些数字（写在前面，免得被误读）：

- **`Structured Output Validity Rate` 稳定 100%**：`chat_json` 的「解析 → 校验 → 错误回灌 →
  重试」状态机在真实流量下没有崩过一例。
- **波动是真的，且不小**：`Plan Success Rate` 同批摆动 ±14.29、`Precedence Accuracy` 在
  8 条样本上摆动曾达 37.5pt。**引用任何数字都要连波动一起引用**，单轮数字不能当结论。
- **指标是拆分过的，才能归因**：`Tool Set`（该调的调了吗）与 `Tool Sequence`（顺序完全一致吗）
  是两个数——前者高于后者说明相当一部分"序列错误"其实是**合法换序**；
  而 `Precedence` 只看声明过的关键先后约束，才能识别出真正的语义顺序错误。
- **端到端要一起读两个数**：`Plan Success Rate`（42.86）是「计划合法 + 答案通过断言」的乘积，
  而开放式用例的 judge 通过率是 82.22 —— 两者口径不同，**不要用其中一个否定另一个**。
- **指标口径随迭代修订过**，历史批次的数字与当前不可直接比较；口径演进记录在
  [`docs/design.md`](docs/design.md) 第 19 节，明细在 `src/first_agent/evals/repeats_live.json`。

### 门禁有效性：用变异测试证明"测试真的会红"

| 注入的缺陷 | 结果 |
|---|---|
| `resolve_arguments` 不解析 `$steps` 引用 | `Argument Accuracy` 恶化 → 门禁失败（exit 1） |
| 把 `conflict` 记忆关系错映射为 `keep` | `Memory Decision Accuracy` 恶化 → 门禁失败（exit 1） |
| 去掉工具调用前的预算检查 | 3 条预算用例失败 |
| 不返回工具观测数据 | 观测层断言全部失败 |

产物落在 `src/first_agent/evals/`：`baseline.json`（离线基线）、`current.json`、`diff.md`
（真 LLM 段为 `*_live.*`）；`--repeat N` 另写 `repeats{suffix}.json`（每轮指标、波动、
跨轮一致率、不稳定用例名单）。每次结果都记录 **git commit / model / prompt version /
tool schema version / case set hash**，可以回答"这次指标变化对应哪次改动"。
`--update-baseline` 会**拒绝**把失败结果写为基线。

---

## CLI 速查

| 命令 | 作用 |
|---|---|
| `first-agent chat "<问题>" [--mode react\|plan] [--user U] [--session S] [--show-thought] [--show-memory] [--budget-ms N]` | 单轮对话（默认 `react`） |
| `first-agent eval [--real] [--judge] [--repeat N] [--update-baseline] [--no-gate] [--quiet] [--out DIR]` | 跑评测与回归门禁；`--real --repeat 3 --judge` 给真 LLM 的均值/波动与跨轮一致率 |
| `first-agent memory list [--user U \| --all-users] [--json]` | 查看长期记忆 |
| `first-agent memory show <key> [--user U]` / `memory rm <key> [--user U]` | 查看 / 删除单条记忆 |
| `first-agent trace stats [--dir DIR] [--json]` / `trace tail [--n N]` | Trace 聚合统计 / 最近事件 |

`memory` / `trace` 只做读取与统计，数据层在 `core/inspect.py`（可直接单测），CLI 只负责打印。

---

## 隐私与 Trace

- Trace 写入 `var/traces/YYYY-MM-DD.jsonl`，默认 `TRACE_MODE=safe`：
  `user_id` / `session_id` 做 hash，手机号、邮箱掩码，`user_name` 一类敏感 key 的值整体掩码。
- 数据库里的 `user_id` 保留明文、Trace 中 hash，是**刻意**的不一致：
  DB 是本地文件，Trace 可能被外发。
- **已知缺口**：`final_answer` 等自由文本只做手机号 / 邮箱掩码，用户自报的姓名可能原样落盘。
  因此 `var/traces/` 不进版本库、也不要外发；本地调试可用 `TRACE_MODE=full`。

---

## 已知限制

诚实地列出来，比让读者自己踩要好。

- **自动 Router 未实现**：模式由用户 `--mode` 显式选择，不做"该用 ReAct 还是 Plan"的自动判定。
- **无 HTTP 服务层**：只有 CLI。服务化是可选阶段，未做——但 `SessionStore` 已按多会话设计，
  接入时不需要改内核。
- **单值记忆**：`UNIQUE(user_id, key)`，不支持多值 KV 与版本历史；
  `complementary` 关系按 `skip` 处理并记 Trace。
- **出行耗时有"真实/估算"两种数据源，由配置决定**：配了 `AMAP_API_KEY` → 高德**真实路径规划**
  （`source: amap-driving` / `amap-transit`，结果含距离与解析出的规范地名）；未配置 → 退化为
  启发式估算（距离 ÷ 速度，默认距离 12km，`source: heuristic`）。**估算值不可信**——
  实测"重庆→成都"会算成 16 分钟，模型自己察觉并拒绝了它。这不该算设计优点：
  静态实现就该把数据来源标出来，让调用方判断。
- **地名不确定时追问，而不是猜**：解析只用高德 **POI 文本搜索**——实测地理编码会把「解放碑」的
  首条给到四川广元的住宅区、把「江北机场」解析到宁波江北区；而 POI 无结果时它**必然**挑一个
  "道路/村庄级"的随意城市（「人民路」→ 陕西商洛，算出 722 公里；「火车站」→ 甘肃陇南徽县
  "火站村"），把"信息不够具体"变成**自信的错误答案**。所以 POI 无结果 → `ambiguous_place` →
  **追问用户补充城市**：plan 模式由引擎转成 `need_info`（并指出该补出发地还是目的地），
  react 模式由可操作的错误信息驱动模型追问。实测"从人民路到观音桥开车要多久"会回答
  "无法确定『人民路』的位置：请补充所在城市或更完整的名称（例如「重庆人民路」）"，
  补一句后即得 13 分钟 / 4.2 公里。结果里带 `origin_resolved` / `destination_resolved` 供核对。
- **步行/骑行有距离上限**：高德返回 `OVER_DIRECTION_RANGE` 时映射为 `not_found`（不重试），
  与真正的服务故障（`unavailable`，可重试）区分开。
- **LLM-as-Judge 的局限**：与作答复用同一客户端（仅 prompt 独立），判定偏宽松；
  `Task Success Rate` 只统计裁判成功的样本。实测 15 条里有 5 条会在轮间翻转
  （其中 2 条与记忆取值直接相关），因此轮间摆动仍可达 20pt——**只能看多轮均值**，
  CLI 摘要会把单条权重与摆动一起打出来。
- **端到端指标受断言口径影响**：`Plan Success Rate` = 计划合法 + 断言通过。
  断言已按证据层重构（见上），但计划类用例仍要求回答命中关键实体，
  **不要把它读成"模型只成功 4 成"**；端到端能力要与 judge 的 82.22 一起看。
- **模型不会从可恢复错误中恢复**：非法参数会返回带**合法取值**的 `invalid_arguments`，
  但真跑中模型**不重试**、直接放弃该工具。加了方式别名后这类错误已归零，
  但"拿到可执行反馈却不用"仍是隐患。
- **夹具世界比模型的动作空间窄**：真跑出现过 `estimate_travel_time: not_found`——
  模型传入的城市/方式不在 Fake provider 覆盖范围。暴露的是夹具覆盖面，不是模型错误。
- **`cl100k_base` 未接入**：上下文预算用字符数近似 + 安全系数 1.2，
  避免首次联网下载词表破坏"离线可跑"。
- **依赖未清理**：`pyproject.toml` 仍声明了早期原型用到的 `flask` / `pyngrok`，主线代码已不使用。

---

## 质量保证

```bash
uv run pytest -q                  # 521 个测试，全部离线（ScriptedLLM + Fake provider）
uv run first-agent eval --quiet   # 离线评测门禁，无需 API Key
```

- 每个里程碑一个 commit，改完跑一次全量测试 + 一次评测门禁。
- 关键保护逻辑都用**变异测试**验证过"测试真的会红"（预算熔断、`$steps` 解析、
  记忆裁决映射、断言证据层）。
- 所有外部 IO 都能注入 Fake：不联网、不需要 Key，也能覆盖失败 / 超时 / 空结果 / 降级路径。
- 真实运行中发现的缺陷（包括**我自己的新规则误拦了正确行为**）都会补成回归测试或文档记录，
  而不是"改完就算"。

---

## 项目结构

```text
src/first_agent/
├── core/        # client / context / session / budget / tracing / db / inspect / engine
├── runtime/     # react.py（ReAct 循环）、plan_execute.py（DAG 编排）、slots.py
├── tools/       # registry.py、executor.py、builtin/（工具契约，不含网络实现）
├── providers/   # 真实与 Fake 外部 IO（天气 / 时间解析 / 日程 / 出行耗时）
├── memory/      # extractor / adjudicator / service / summarizer / store / migrate（表结构迁移）
├── evals/       # cases.jsonl、judge_cases.jsonl、offline/live/suite/judge.py、baseline*/current*/diff*/
└── main.py      # CLI：chat / eval / memory / trace
tests/           # 521 个离线测试
docs/design.md   # 设计文档：契约、决策记录、里程碑与实测结论
```

仓库根目录还有上一代原型的快照与笔记：`archive/`、`templates/index.html`、`REVIEW.md`、
`scripts/migrate_memory_v1.py`（把旧版 `memory.db` 迁到当前表结构的一次性脚本）。
它们是历史产物，**与主线代码和测试无关**，可以安全忽略。

---

## 深入阅读

- [`docs/design.md`](docs/design.md)：分层契约、状态机、决策记录（含"实现与原设计的偏差"登记）、
  分阶段里程碑、以及每个阶段的真实实测结论与归因。
- 想快速了解"评测为什么这么设计"，看 design.md 第 16 节；想看历史口径演进与踩坑记录，看第 19 节。

---

## 附：简历描述（作者自用，可忽略）

> **天气出行助手（Python，Agent 运行时从零实现）**：一个能查天气 / 日程 / 两地耗时 / 时间的
> 出行助手，其 Function Calling 运行时没有用任何 Agent 框架——手写 ReAct 与
> Plan-Execute（依赖 DAG + 跨步骤引用）双模式、三层记忆与 SQLite 会话隔离、
> 耗时预算熔断与失败降级；配 36 条离线评测与 baseline 回归门禁，改代码后自动判定指标退化。

- 用原生 `tools + tool_calls` 替代自由 JSON Action，以 `call_id` 对齐并行工具结果，
  避免自造协议带来的参数抽取精度损失。
- 把"顺序对不对"拆成四个互补指标（召回 / 关键先后 / 计划合法性 / 端到端）：
  实测 `Tool Set` 90.7 而 `Tool Sequence` 71.1，相当一部分"序列错误"其实是合法换序，
  单指标会把两类问题混成一个数字。
- 把评测断言从「回答措辞」重写到「证据层」（工具观测 / 结构化槽位 / 错误码 / 确定性输出），
  并让失败报告指明具体断言；`Assertion Pass Rate` 53.3 → 71.1，残留失败经逐条归因
  **全部是模型行为问题**——说明此前这项指标度量的是措辞而非能力。该重构还暴露了两个工具契约
  缺陷（自由字符串 `mode`、无格式校验的日期），收紧为枚举与 `pattern`，
  把「静默的错误答案」变成显式错误。
- 用评测数据驱动运行时改进：加规划前置校验（缺槽位先追问、不拿编造参数打外部服务）、
  复合意图覆盖（连接词分解需求 + 一次修复性重规划）、出行方式别名归一化。
  真实 3 轮 `Plan Success Rate` 23.8 → 42.9、多余工具调用率 7.8 → 0.9；
  过程中发现新规则**误拦了"模型自己算出日期"这种正确行为**，据此把校验收窄为只查地点——
  这类误伤只有真实运行才暴露。
- 记忆写入拆成「抽取 → 关系裁决 → 确定性落库」，LLM 只判关系、不直接决定写库，
  从结构上消除"判了 add 但代码没处理导致数据静默丢失"的缺陷。
- 建立**两段式评测**（离线夹具硬门禁 + 真 LLM 波动指标）与方向化容差判定，
  并用变异测试证明门禁真能挡住回归；真 LLM 段 `--repeat 3` 量化出模型能力**及其波动**，
  并对开放式用例接入 LLM-as-Judge（15 条样本，单条权重 6.7pt）。
- 521 个测试全部离线可跑（注入 ScriptedLLM 与 Fake provider），覆盖失败、超时、
  依赖阻塞、预算熔断与降级路径。

---

## Windows 注意（输出编码）

`first-agent eval > report.txt` 或 `first-agent eval | Select-String ...` 时，Python 的 stdout 会从
"Windows 控制台 API" 退化为 **ANSI 代码页**（简中为 `gbk`），此时打印 `✅ / ✗ / ⚠ / ✓`
会直接 `UnicodeEncodeError`：**命令 exit 1、输出文件全空**。CLI 启动时会 `_configure_stdio()`：

- **重定向 / 管道输出** → 切成 UTF-8（与 `diff.md`、JSON 产物一致），重定向出来就是正确的 UTF-8；
- **交互式控制台** → 保留控制台自身编码，只把 `errors` 设为 `replace`，中文照常显示，
  底层不支持的符号降级为 `?` 而不是抛异常；
- 想让控制台也按 UTF-8 显示（例如看清 `✅/✗`），可设 `PYTHONUTF8=1` 或先执行 `chcp 65001`。

回归测试在 `tests/test_cli_encoding.py`：先用 `PYTHONIOENCODING=gbk` **证明"不修必崩"**，
再验证修复后的行为——这个"前提测试"保证它不会变成永远假装通过的空测试。
