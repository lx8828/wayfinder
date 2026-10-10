# 评测对比（real）

- 生成时间：2026-10-10T13:53:41+00:00
- git commit：c947998
- model：deepseek-chat
- case set hash：7af1d9cc897e
- prompt version：f61d6c635cb9
- tool schema version：b0438bbb98a6

## 指标对比

（无基线，已跳过对比）

## 门禁

通过 ✅

## 失败用例

- react_single_weather_memory_002: tool_set=✗ | 实际调用: get_weather,get_current_time
- react_single_datetime_004: tool_set=✗; assertions=✗ | 实际调用: resolve_datetime,get_current_time
- react_single_travel_005: tool_set=✗; tool_recall=✗; arguments=✗; assertions=✗ | 实际调用: （无工具调用）
- plan_single_time_008: tool_recall=✗; tool_sequence=✗; plan_validity=✗; plan_success=✗; assertions=✗ | 实际调用: （无工具调用）
- react_double_weather_travel_010: memory_delta=✗ | 实际调用: get_weather,estimate_travel_time
- plan_double_time_weather_011: memory_delta=✗; plan_validity=✗; plan_success=✗ | 实际调用: get_current_time,get_weather
- plan_double_datetime_weather_012: memory_delta=✗; plan_success=✗; assertions=✗ | 实际调用: resolve_datetime,get_weather
- plan_double_calendar_013: tool_recall=✗; tool_sequence=✗; precedence=✗; arguments=✗; plan_validity=✗; plan_success=✗; assertions=✗; status=need_info(期望 success) | 实际调用: （无工具调用）
- react_double_travel_time_014: memory_delta=✗ | 实际调用: estimate_travel_time,get_current_time
- plan_chain_park_015: plan_success=✗; assertions=✗ | 实际调用: resolve_datetime,get_weather,get_calendar_events
- plan_chain_travel_016: tool_sequence=✗; precedence=✗; memory_delta=✗; tool_ok=✗; plan_validity=✗; plan_success=✗; assertions=✗; tool_errors=[estimate_travel_time:not_found] | 实际调用: resolve_datetime,estimate_travel_time,get_weather
- react_chain_calendar_017: tool_set=✗; tool_recall=✗; precedence=✗; arguments=✗; assertions=✗ | 实际调用: get_current_time,get_calendar_events,get_calendar_events
- …另有 8 条失败用例未列出
