"""
日期发现与幂等判定。

核心职责：
  1. 扫描 src_dir，发现所有目标日期（复用 gfs_to_15min.scan_data_dirs）
  2. 判断某日期是否需要（重新）处理：
       - CSV 不存在
       - 状态未记录或为 failed
       - 输入目录 mtime 与上次处理时不同（输入有更新）
  3. 计算某日期的输入目录 mtime 快照（用于幂等判定）
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 将项目根目录加入 sys.path，确保 gfs_to_15min 可以被 import
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from gfs_to_15min import dirs_for_date, parse_dir_init_time, scan_data_dirs

from .config import SourceMapping


# ---------------------------------------------------------------------------
# 目标日期发现
# ---------------------------------------------------------------------------

def all_target_dates(mapping: SourceMapping) -> list[date]:
    """
    扫描 src_dir，返回所有有数据覆盖的目标日期（排序）。

    逻辑：每个 init 目录覆盖 init_time 到 init_time+23h，
    对应的 UTC 日期集合即为目标日期。
    """
    if not mapping.src_dir.is_dir():
        return []
    all_dirs = scan_data_dirs(mapping.src_dir)
    dates: set[date] = set()
    for init_time, _ in all_dirs:
        for h in range(24):
            dates.add((init_time + timedelta(hours=h)).date())
    return sorted(dates)


def dates_from_init_time(init_time: datetime) -> list[str]:
    """
    给定单个 init_time，返回它覆盖的所有 YYYY-MM-DD 字符串。
    用于 watchdog 事件触发时快速计算受影响日期。
    """
    dates = set()
    for h in range(24):
        dates.add((init_time + timedelta(hours=h)).date().isoformat())
    return sorted(dates)


# ---------------------------------------------------------------------------
# 输入目录 mtime 快照
# ---------------------------------------------------------------------------

def input_mtimes_for_date(mapping: SourceMapping, date_str: str) -> dict[str, float]:
    """
    返回覆盖 date_str 的所有 init 目录的 mtime 字典 {dirname: mtime}。
    当目录不存在时跳过（不报错）。
    """
    if not mapping.src_dir.is_dir():
        return {}
    all_dirs = scan_data_dirs(mapping.src_dir)
    target = date.fromisoformat(date_str)
    relevant = dirs_for_date(all_dirs, target)
    return {
        p.name: p.stat().st_mtime
        for _, p in relevant
        if p.exists()
    }


# ---------------------------------------------------------------------------
# 幂等判定
# ---------------------------------------------------------------------------

def needs_processing(
    mapping: SourceMapping,
    date_str: str,
    state: dict,
) -> bool:
    """
    返回 True 表示该日期需要（重新）处理。

    判定规则（满足任一即需处理）：
      1. 目标 CSV 不存在
      2. 状态条目不存在或 status != "done"
      3. 当前输入目录 mtime 与状态中记录的不同（输入有更新）
    """
    csv_path = mapping.dst_dir / f"gfs_15min_{date_str}.csv"
    if not csv_path.exists():
        return True

    entry = state.get(mapping.name, {}).get(date_str)
    if not entry or entry.get("status") != "done":
        return True

    # mtime 检查：输入是否有更新
    saved_mtimes   = entry.get("input_mtimes", {})
    current_mtimes = input_mtimes_for_date(mapping, date_str)
    if current_mtimes != saved_mtimes:
        return True

    return False
