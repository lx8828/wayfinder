# 评测对比（real）

- 生成时间：2026-10-09T20:03:09+00:00
- git commit：928509d
- model：deepseek-chat
- case set hash：6612a021f59b
- prompt version：f61d6c635cb9
- tool schema version：b0438bbb98a6

## 指标对比

（无基线，已跳过对比）

## 门禁

通过 ✅

## 失败用例

- react_single_datetime_004: tool_set=✗; assertions=✗ | 实际调用: get_current_time,resolve_datetime
- react_single_travel_005: memory_delta=✗ | 实际调用: estimate_travel_time
- react_double_weather_travel_010: memory_delta=✗ | 实际调用: get_weather,estimate_travel_time
- plan_double_time_weather_011: memory_delta=✗ | 实际调用: get_current_time,get_weather
- plan_double_datetime_weather_012: assertions=✗ | 实际调用: resolve_datetime,get_weather
- plan_double_calendar_013: tool_sequence=✗; arguments=✗; assertions=✗ | 实际调用: （无工具调用）
- react_double_travel_time_014: memory_delta=✗ | 实际调用: estimate_travel_time,get_current_time
- plan_chain_park_015: assertions=✗ | 实际调用: resolve_datetime,get_weather,get_calendar_events
- plan_chain_travel_016: tool_sequence=✗; memory_delta=✗; tool_ok=✗; assertions=✗ | 实际调用: resolve_datetime,estimate_travel_time,get_weather
- react_chain_calendar_017: assertions=✗ | 实际调用: get_current_time,resolve_datetime,get_calendar_events
- plan_chain_full_018: memory_delta=✗; tool_ok=✗; assertions=✗ | 实际调用: resolve_datetime,estimate_travel_time,get_weather,get_calendar_events
- plan_need_info_city_020: assertions=✗ | 实际调用: （无工具调用）
- …另有 5 条失败用例未列出
