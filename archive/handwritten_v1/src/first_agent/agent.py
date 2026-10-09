import json
from first_agent.state import AgentState
from first_agent.memory import load_memory, load_memory_into_state, extract_memory, remember
from first_agent.executor import execute_plan

class Agent:
    def __init__(self, client, tools_schemas, tool_registry, system_prompt):
        """
        Agent 骨架类。

        :param client: LLM API 客户端实例
        :param tools_schemas: 工具描述列表 (List[Dict])
        :param tool_registry: 工具函数注册表 (Dict)
        :param system_prompt: 系统提示词，可包含业务逻辑
        """
        self.client = client
        self.tools = tools_schemas
        self.tool_registry = tool_registry
        self.messages = [{"role": "system", "content": system_prompt}]

    def run(self):
        """主循环入口"""
        print("=" * 40)
        print("  AI Agent 已启动")
        print("  1. ReAct 模式")
        print("  2. Plan-and-Execute 模式")
        print("=" * 40)

        mode = input("请选择模式 (1/2): ")
        state = AgentState()
        load_memory_into_state(state)

        while True:
            user_input = input("\n你：")
            if user_input.lower() == "exit":
                break

            if mode == "2":
                self.run_plan_execute(user_input)
            else:
                self.run_react(user_input)

            # 通用记忆钩子：每轮对话后自动处理记忆
            self._memory_hook(user_input)

    def chat(self, user_input, mode="1"):
        """
        Web 调用入口。返回 AI 回答。
        
        Args:
            user_input: 用户输入
            mode: "1"=ReAct, "2"=Plan-Execute
        
        Returns:
            AI 回答文本
        """
        if mode == "2":
            answer = self._run_plan_execute(user_input)
        else:
            answer = self._run_react(user_input)
        
        # 记忆钩子
        self._memory_hook(user_input)
        return answer

    def _run_react(self, user_input):
        """标准 ReAct 循环，返回 AI 回答"""
        self.messages.append({"role": "user", "content": user_input})

        max_steps = 5
        step = 0
        answer = ""

        while True:
            step += 1
            if step > max_steps:
                answer = "执行步骤过多，请重新描述需求。"
                break

            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=self.messages,
                tools=self.tools,
                tool_choice="auto"
            )

            message = response.choices[0].message

            if not message.tool_calls:
                answer = message.content
                break

            self.messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments
                        }
                    }
                    for tc in message.tool_calls
                ]
            })

            for tool_call in message.tool_calls:
                result = self._execute_tool(tool_call)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": result
                })

        self.messages.append({
            "role": "assistant",
            "content": answer
        })
        return answer

    def _run_plan_execute(self, user_input):
        """Plan-and-Execute 循环，返回 AI 回答"""
        # 每轮重置 state 并加载记忆
        state = AgentState()
        load_memory_into_state(state)

        self.messages.append({"role": "user", "content": user_input})

        # 业务扩展点 1: 状态提取
        if hasattr(self, '_extract_state'):
            new_state = self._extract_state(user_input)
            for key, value in new_state.model_dump().items():
                if value:
                    state.set(key, value)

        # 业务扩展点 2: 任务规划
        if hasattr(self, '_plan'):
            plan = self._plan(state)

            if plan.need_info:
                answer = f"我还需要知道你的{'、'.join(plan.need_info)}，才能帮你判断。"
                self.messages.append({"role": "assistant", "content": answer})
                return answer

            if plan.steps:
                results = execute_plan(plan, state, self.tool_registry)

                self.messages.append({
                    "role": "user",
                    "content": f"当前状态: {json.dumps(state.show(), ensure_ascii=False)}\n工具执行结果: {json.dumps(results, ensure_ascii=False)}"
                })

                response = self.client.chat.completions.create(
                    model="deepseek-chat",
                    messages=self.messages
                )
                answer = response.choices[0].message.content
            else:
                answer = "我暂时无法处理你的请求，请告诉我更多信息。"
        else:
            # 默认行为：直接请求 LLM
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=self.messages
            )
            answer = response.choices[0].message.content

        self.messages.append({"role": "assistant", "content": answer})
        return answer

    def run_react(self, user_input):
        """标准 ReAct 循环（命令行版）"""
        answer = self._run_react(user_input)
        print("\nAI：")
        print(answer)

    def run_plan_execute(self, user_input):
        """Plan-and-Execute 循环（命令行版）"""
        answer = self._run_plan_execute(user_input)
        print("\nAI：")
        print(answer)

    def _execute_tool(self, tool_call):
        """内部工具执行逻辑"""
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)
        tool = self.tool_registry.get(name)
        if not tool:
            return "没有找到该工具"
        return tool(**args)

    def _memory_hook(self, user_input):
        """内部记忆处理钩子"""
        memory = extract_memory(self.client, user_input)
        for item in memory.get("memories", []):
            result = remember(self.client, item)
            if not result:
                continue
            action = result.get("action", "?")
            if action == "update":
                shown_value = result.get("new_value", item.get("value", ""))
            elif action == "add":
                shown_value = item.get("value", "")
            else:  # keep
                shown_value = "(保持不变)"
            print(f"  记忆[{action}]: {item['key']} = {shown_value}")
