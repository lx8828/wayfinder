import inspect


def execute_plan(plan, state, tool_registry):

    results = {}

    for step in plan.steps:

        func = tool_registry.get(step)
        if not func:
            results[step] = f"未找到工具: {step}"
            continue

        sig = inspect.signature(func)
        kwargs = {}

        for param_name in sig.parameters:
            value = state.get(param_name)
            if value is not None:
                kwargs[param_name] = value

        results[step] = func(**kwargs)

    return results
