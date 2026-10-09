from first_agent.models import TravelState

def extract_state(client, user_input):

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role":"system",
                "content":
                """
你负责提取用户出行信息。

只返回json格式，字段如下：

destination: 目的地
time: 时间
purpose: 目的

没有的信息填null。
"""
            },
            {
                "role":"user",
                "content":user_input
            }
        ],
        response_format={
            "type":"json_object"
        }
    )


    data = response.choices[0].message.content


    return TravelState.model_validate_json(data)