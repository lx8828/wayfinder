from first_agent.memory.manager import get_all_memory

KEY_MAP = {
    "用户城市": "destination",
    "居住地": "destination",
    "用户姓名": "user_name",
}


def load_memory():
    memories = get_all_memory()
    result = {}
    for key, value in memories:
        result[key] = value
    return result


def load_memory_into_state(state):
    memories = load_memory()
    for key, value in memories.items():
        mapped_key = KEY_MAP.get(key, key)
        if not state.get(mapped_key):
            state.set(mapped_key, value)
    return state
