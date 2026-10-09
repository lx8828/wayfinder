import json


def decide_memory_update(client, old_memory, new_memory):

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role":"system",
                "content":"""
你是一个记忆管理器。

你的任务：
判断新的用户信息是否应该更新旧记忆。

规则：

如果新信息和旧信息冲突：
返回 update

如果旧信息仍然有效：
返回 keep

如果只是新增信息：
返回 add


只返回JSON。


格式：

{
"action":"update",
"reason":"",
"new_value":""
}


或者：

{
"action":"keep"
}


或者：

{
"action":"add"
}

"""
            },
            {
                "role":"user",
                "content":f"""
旧记忆:

{json.dumps(old_memory,ensure_ascii=False)}


新记忆:

{json.dumps(new_memory,ensure_ascii=False)}

"""
            }
        ]
    )


    return json.loads(
        response.choices[0].message.content
    )