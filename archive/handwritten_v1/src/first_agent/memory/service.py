from first_agent.memory.manager import (
    get_memory,
    update_memory
)

from first_agent.memory.writer import save_memory

from first_agent.memory.update import decide_memory_update



def remember(client, memory):


    if not memory.get("key"):
        return



    key = memory["key"]


    old = get_memory(key)


    # 没有旧记忆

    if not old:

        save_memory(memory)

        return {
            "action":"add"
        }



    # 有旧记忆,组装完整记录(含 category)交给 LLM 判断

    old_memory = {
        "key":key,
        "value":old["value"],
        "category":old["category"]
    }


    result = decide_memory_update(
        client,
        old_memory,
        memory
    )


    if result["action"] == "update":

        update_memory(
            key,
            result["new_value"],
            memory.get("category")
        )


    return result