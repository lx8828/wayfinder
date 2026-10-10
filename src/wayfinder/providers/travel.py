"""出行耗时估算。对应 design.md 11.5 的 ``estimate_travel_time``。

项目没有接真实路况 API，这里给的是**确定性的启发式实现**（距离 ÷ 速度）：
它的价值在于让 Plan 的「多参数 + 依赖链」场景可以真实跑通并被稳定评测，
而不是假装有实时路况。
"""

from __future__ import annotations

from typing import Any

from wayfinder.providers.base import ProviderNotFound

# 支持的方式必须与工具契约的枚举**完全一致**（`TravelMode`）：加枚举忘了加这里，
# 会让一个合法的出行方式在 provider 层变成 not_found —— 由一致性测试钉住。
MODE_SPEED_KMH: dict[str, float] = {
    "walk": 5.0,
    "bike": 15.0,
    "bus": 25.0,
    "subway": 35.0,
    "car": 45.0,
    "taxi": 40.0,
    "train": 90.0,
}
DEFAULT_DISTANCE_KM = 12.0


class HeuristicTravelProvider:
    def __init__(self, *, distance_km: float | None = None) -> None:
        self.distance_km = distance_km

    def estimate(self, origin: str, destination: str, mode: str) -> dict[str, Any]:
        return self._estimate(origin, destination, mode)

    def _estimate(self, origin: str, destination: str, mode: str) -> dict[str, Any]:
        mode_key = (mode or "").strip().lower()
        speed = MODE_SPEED_KMH.get(mode_key)
        if speed is None:
            raise ProviderNotFound(
                f"不支持的出行方式：{mode}（可选：{'/'.join(sorted(MODE_SPEED_KMH))}）"
            )
        minutes = max(1, round((self.distance_km or DEFAULT_DISTANCE_KM) / speed * 60))
        return {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "duration_minutes": int(minutes),
            "source": "heuristic",
        }
