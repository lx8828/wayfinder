# 评测对比（real）

- 生成时间：2026-10-10T15:02:50+00:00
- git commit：cce6aec
- model：deepseek-chat
- case set hash：d70b65d8e944
- prompt version：5d942838aae5
- tool schema version：42292703d006

## 指标对比

（无基线，已跳过对比）

## 门禁

通过 ✅

## 失败用例

- react_single_travel_005: memory_delta=✗ | 实际调用: estimate_travel_time
- plan_single_time_008: tool_recall=✗; tool_sequence=✗; plan_validity=✗; plan_success=✗; assertions=✗[observation:contains(16:30)、final_answer∈[16:30/4:30/四点半]]; status=need_info(期望 success) | 实际调用: （无工具调用）
- react_double_weather_travel_010: memory_delta=✗; assertions=✗[observation:contains(320)] | 实际调用: get_weather,estimate_travel_time
- plan_double_time_weather_011: memory_delta=✗ | 实际调用: get_current_time,get_weather
- plan_double_calendar_013: plan_success=✗; assertions=✗[observation:contains(项目评审会)] | 实际调用: resolve_datetime,get_calendar_events
- react_double_travel_time_014: memory_delta=✗ | 实际调用: estimate_travel_time,get_current_time
- plan_chain_park_015: tool_recall=✗; tool_sequence=✗; precedence=✗; arguments=✗; plan_validity=✗; plan_success=✗; assertions=✗[observation:contains(项目评审会)、final_answer∈[评审/会议/冲突]]; status=need_info(期望 success) | 实际调用: （无工具调用）
- plan_chain_travel_016: tool_sequence=✗; precedence=✗; memory_delta=✗; plan_validity=✗; plan_success=✗ | 实际调用: resolve_datetime,estimate_travel_time,get_weather
- react_chain_calendar_017: assertions=✗[observation:contains(晨会)、final_answer∈[晨会/会议/9]] | 实际调用: get_current_time,resolve_datetime,get_calendar_events
- plan_chain_full_018: memory_delta=✗; plan_success=✗; assertions=✗[observation:contains(项目评审会)] | 实际调用: resolve_datetime,estimate_travel_time,get_weather,get_calendar_events
- plan_need_info_city_date_022: plan_success=✗; assertions=✗[need_info:contains(destination)] | 实际调用: （无工具调用）
- fail_calendar_plan_026: assertions=✗[tool_error:contains(unavailable)、final_answer∈[无法/失败/稍后]] | 实际调用: resolve_datetime,get_calendar_events
- …另有 2 条失败用例未列出
