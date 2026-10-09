import os
import httpx
from dotenv import load_dotenv
from first_agent.tools.base import tool

load_dotenv()

API_HOST = os.getenv("QWEATHER_HOST")
API_KEY = os.getenv("QWEATHER_API_KEY")


def _get_location_id(destination):
    """通过 GeoAPI 查询城市名对应的 LocationID"""
    url = f"https://{API_HOST}/geo/v2/city/lookup"
    headers = {"X-QW-Api-Key": API_KEY}
    params = {"location": destination}
    r = httpx.get(url, headers=headers, params=params, timeout=5)
    data = r.json()

    if r.status_code != 200 or "location" not in data or not data["location"]:
        return None

    return data["location"][0]["id"]

@tool(
        description="查询指定城市天气"
)
def get_weather(destination: str):
    """
    查询指定城市的实况天气

    Args:
        destination: 城市名称，例如重庆、北京、上海
    """
    try:
        loc_id = _get_location_id(destination)
        if not loc_id:
            return f"未找到城市：{destination}，请尝试更准确的名称"

        url = f"https://{API_HOST}/v7/weather/now"
        headers = {"X-QW-Api-Key": API_KEY}
        params = {"location": loc_id}
        r = httpx.get(url, headers=headers, params=params, timeout=5)
        data = r.json()

        if r.status_code != 200 or "now" not in data:
            return f"查询失败：{data}"

        now = data["now"]
        return (
            f"{destination}当前天气：{now['text']}，"
            f"气温{now['temp']}°C（体感{now['feelsLike']}°C），"
            f"湿度{now['humidity']}%，"
            f"{now['windDir']}风{now['windScale']}级"
        )
    except Exception as e:
        return f"查询失败：{e}"
