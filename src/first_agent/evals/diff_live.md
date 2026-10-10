# 评测对比（real）

- 生成时间：2026-10-10T14:14:31+00:00
- git commit：137aad6
- model：deepseek-chat
- case set hash：d70b65d8e944
- prompt version：f61d6c635cb9
- tool schema version：566dd76b7002

## 指标对比

（无基线，已跳过对比）

## 门禁

通过 ✅

## 失败用例

- react_single_datetime_004: tool_set=✗ | 实际调用: get_current_time,resolve_datetime
- react_single_travel_005: tool_set=✗; tool_recall=✗; arguments=✗; memory_delta=✗; assertions=✗[observation:contains(320)] | 实际调用: （无工具调用）
- plan_single_time_008: plan_success=✗; assertions=✗[final_answer∈[16:30/4:30/四点半]] | 实际调用: get_current_time
- react_double_weather_travel_010: memory_delta=✗ | 实际调用: get_weather,estimate_travel_time
- plan_double_time_weather_011: memory_delta=✗; plan_validity=✗; plan_success=✗ | 实际调用: get_current_time,get_weather
- plan_double_calendar_013: plan_success=✗; assertions=✗[observation:contains(项目评审会)] | 实际调用: resolve_datetime,get_calendar_events
- react_double_travel_time_014: memory_delta=✗ | 实际调用: estimate_travel_time,get_current_time
- plan_chain_park_015: plan_success=✗; assertions=✗[observation:contains(项目评审会)] | 实际调用: resolve_datetime,get_weather,get_calendar_events
- plan_chain_travel_016: tool_sequence=✗; precedence=✗; memory_delta=✗; tool_ok=✗; plan_validity=✗; plan_success=✗; assertions=✗[observation:contains(320)]; tool_errors=[estimate_travel_time:invalid_arguments] | 实际调用: resolve_datetime,estimate_travel_time,get_weather
- react_chain_calendar_017: assertions=✗[observation:contains(晨会)] | 实际调用: get_current_time,resolve_datetime,get_calendar_events
- plan_chain_full_018: tool_sequence=✗; memory_delta=✗; tool_ok=✗; plan_validity=✗; plan_success=✗; assertions=✗[observation:contains(项目评审会)、observation:contains(320)]; tool_errors=[estimate_travel_time:invalid_arguments] | 实际调用: resolve_datetime,estimate_travel_time,get_weather,get_weather,get_calendar_events
- plan_need_info_city_020: plan_success=✗; assertions=✗[need_info:contains(destination)] | 实际调用: （无工具调用）
- …另有 6 条失败用例未列出
