from first_agent.tools.base import tools


tool_registry = {}


for func in tools:

    tool_registry[func.__name__] = func