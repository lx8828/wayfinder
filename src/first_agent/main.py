import os
import json
from openai import OpenAI
from dotenv import load_dotenv

from first_agent.agent import Agent
from first_agent.tools.base import get_tool_schemas
from first_agent.tools.registry import tool_registry
from first_agent.state_manager import extract_state
from first_agent.planner import create_plan
from first_agent.memory import load_memory

load_dotenv()

# ==========================================
# 业务逻辑层：出行助手
# ==========================================

class TravelAssistant(Agent):
    def __init__(self, client, tools_schemas, tool_registry):
        # 1. 构建业务专属的 System Prompt
        system_prompt = self._build_system_prompt()
        super().__init__(client, tools_schemas, tool_registry, system_prompt)

    def _build_system_prompt(self):
        user_memory = load_memory()
        return f"""
你是一名智能出行助手。
你的目标是帮助用户判断是否适合出门。
用户信息：
{json.dumps(user_memory, ensure_ascii=False)}
如果相关，可以使用这些信息。
不要展示内部思考过程。
直接给用户自然、简洁的建议。
"""

    # 2. 重写状态提取逻辑（Plan-Execute 模式下会自动调用）
    def _extract_state(self, user_input):
        return extract_state(self.client, user_input)

    # 3. 重写规划逻辑（Plan-Execute 模式下会自动调用）
    def _plan(self, state):
        return create_plan(
            self.client,
            {
                "state": state.show(),
                "memory": load_memory()
            },
            self.tools
        )

# ==========================================
# 程序入口
# ==========================================

def main():
    client = OpenAI(
        api_key=os.getenv("DEEPSEEK_API_KEY"),
        base_url="https://api.deepseek.com"
    )
    tools_schemas = get_tool_schemas()
    
    # 实例化业务 Agent
    assistant = TravelAssistant(client, tools_schemas, tool_registry)
    
    # 启动
    assistant.run()

if __name__ == "__main__":
    main()
