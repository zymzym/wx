"""
"获取最近 8 个 date_cycle" 接口协议。

此模块定义协议（Protocol）和本地兜底实现（fallback_latest_8）。
生产部署时，在 main.py 中将 get_latest 替换为对接真实上游服务的实现，
例如：HTTP 请求、数据库查询、消息队列消费等。

接口契约：
  - 返回最多 8 个 (date_str, cycle) 元组，按时间倒序排列（最新在前）
  - date_str 格式："YYYY-MM-DD"
  - cycle    格式："00" | "06" | "12" | "18"
"""

from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class LatestDateCyclesSource(Protocol):
    def __call__(self) -> list[tuple[str, str]]:
        """返回最近 8 个 (date, cycle) 列表。"""
        ...


def fallback_latest_8() -> list[tuple[str, str]]:
    """
    本地兜底实现：从系统时钟推算最近 8 个已发布的 GFS cycle。

    规则：GFS cycle 在起报时刻约 4 小时后发布。
    此实现无需网络请求，适合在无法访问上游时保障基本运行。

    生产环境建议替换为真实上游数据源。
    """
    LAG_HOURS = 5   # 保守估计发布延迟
    now       = datetime.now(timezone.utc) - timedelta(hours=LAG_HOURS)

    result: list[tuple[str, str]] = []
    dt = now
    while len(result) < 8:
        cycle_hour = (dt.hour // 6) * 6
        date_str   = dt.strftime("%Y-%m-%d")
        cycle_str  = f"{cycle_hour:02d}"
        result.append((date_str, cycle_str))
        dt -= timedelta(hours=6)

    return result
