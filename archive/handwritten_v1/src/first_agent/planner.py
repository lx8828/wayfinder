import json
from first_agent.models import Plan


def create_plan(client, state, tools):

    tool_descriptions = "\n".join([
        f"{i+1}. {t['function']['name']}\n{t['function']['description']}"
        for i, t in enumerate(tools)
    ])

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": f"""
你是一个任务规划器。

你的任务：
根据用户当前状态和长期用户信息，判断完成任务需要哪些步骤。

当前支持工具：

{tool_descriptions}


规则：

用户记忆中的城市可以作为默认目的地。
如果用户没有提供destination，可以使用记忆中的城市。

如果用户需要出门：
需要考虑天气和时间。

如果缺少关键条件：
返回需要补充的信息。

只返回JSON。

格式：

{{
"need_info": [],
"steps": []
}}


例如：

状态：
{{
"purpose":"看电影",
"destination":"重庆"
}}

返回：

{{
"need_info":[],
"steps":[
"get_current_time",
"get_weather"
]
}}


如果：

状态：
{{
"purpose":"看电影"
}}

返回：

{{
"need_info":[
"目的地"
],
"steps":[]
}}
"""
            },
            {
                "role": "user",
                "content": json.dumps(
                    state,
                    ensure_ascii=False
                )
            }
        ]
    )

    return Plan.model_validate_json(
       response.choices[0].message.content
    )
