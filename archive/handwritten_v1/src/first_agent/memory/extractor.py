import json


def extract_memory(client, user_input):

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": """
你是一个记忆提取器。

你的任务：
从用户输入中提取值得长期保存的信息，每条信息单独拆开。

值得保存的信息包括：

1. 用户身份（姓名、职业等）
2. 用户偏好（饮食、出行等）
3. 用户习惯（作息、周末活动等）
4. 长期计划

不要保存：
- 一次性事件（如"昨天去看了电影"）
- 临时天气
- 普通聊天
- 当前任务过程

重要：每条信息单独提取，不要合并。
例如"我叫小龙，住在重庆"应该提取为两条：姓名和居住地。

key 必须使用以下标准名称：
- 姓名 → "用户姓名"
- 城市/居住地 → "居住地"
- 饮食偏好 → "饮食偏好"
- 出行偏好 → "出行偏好"
- 作息习惯 → "作息习惯"
- 长期计划 → "长期计划"

只返回JSON：

{
    "memories": [
        {"key": "用户姓名", "value": "小龙", "category": "用户身份"},
        {"key": "居住地", "value": "重庆", "category": "用户身份"}
    ]
}

如果没有值得保存的信息，返回：

{
    "memories": []
}

不要输出其他内容。
"""
            },
            {
                "role": "user",
                "content": user_input
            }
        ]
    )


    return json.loads(
        response.choices[0].message.content
    )
