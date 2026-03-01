"""
完整性判定：以文件系统为唯一事实来源，不依赖任何外部状态。

判定标准（对任意 init_time）：
  1. init 目录存在
  2. manifest.json 存在
  3. fh_start … fh_end 每个文件都存在且 size > 0
"""

from pathlib import Path
from .config import ROOT


def init_dir(out_dir: str, date: str, cycle: str) -> Path:
    """返回对应 init_time 的目录路径（不保证存在）。"""
    date_str  = date.replace("-", "")
    init_time = f"{date_str}T{cycle}00Z"
    return ROOT / out_dir / init_time


def is_complete(out_dir: str, date: str, cycle: str,
                fh_start: int, fh_end: int) -> bool:
    """
    返回 True 当且仅当 [fh_start, fh_end] 所有文件存在且 size > 0。
    纯只读操作，可高频调用。
    """
    d = init_dir(out_dir, date, cycle)

    if not d.is_dir():
        return False

    if not (d / "manifest.json").exists():
        return False

    for fh in range(fh_start, fh_end + 1):
        fname = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
        p     = d / fname
        if not p.exists() or p.stat().st_size == 0:
            return False

    return True


def missing_fh(out_dir: str, date: str, cycle: str,
               fh_start: int, fh_end: int) -> list[int]:
    """返回缺失或大小为 0 的 fh 列表（用于诊断日志）。"""
    d      = init_dir(out_dir, date, cycle)
    result = []
    for fh in range(fh_start, fh_end + 1):
        fname = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
        p     = d / fname
        if not p.exists() or p.stat().st_size == 0:
            result.append(fh)
    return result
