from pydantic import BaseModel


class TravelState(BaseModel):
    """
    用户当前出行状态
    """

    destination: str | None = None

    time: str | None = None

    purpose: str | None = None


class Plan(BaseModel):

    need_info: list[str] = []

    steps: list[str] = []