from datetime import datetime

from first_agent.tools.base import tool

@tool(description="获取当前时间")
def get_current_time():

    now = datetime.now()

    return now.strftime(
        "%Y-%m-%d %H:%M:%S"
    )