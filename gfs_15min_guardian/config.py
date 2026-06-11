"""
全局配置：目录映射、调度间隔、并发、日志、状态文件。

所有参数均有默认值，可通过环境变量或 CLI 参数覆盖（CLI 优先级最高）。

环境变量一览：
  GFS15M_ENABLED         设为 1 时启用自动 CSV 转换，默认 0（停用）
  GFS15M_SCAN_INTERVAL   定时扫描间隔（秒），默认 300
  GFS15M_WORKERS         并行转换 worker 数，默认 2
  GFS15M_NO_WATCH        设为 1 时禁用 watchdog，默认 0（启用）
  GFS15M_LOG_DIR         日志目录，默认 <ROOT>/logs
  GFS15M_STATE_FILE      状态文件路径，默认 <LOG_DIR>/gfs_15min_state.json
  GFS15M_MAPPING         目录映射，格式: name:src:dst,name:src:dst
                         路径可为绝对路径或相对于 ROOT 的相对路径
                         默认: jiangsu:data_js:data_js_csv,sichuan:data_sc:data_sc_csv,ningxia:data_nx:data_nx_csv
"""

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录（wx/）
ROOT: Path = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 目录映射
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceMapping:
    """一组输入→输出目录的转换映射。"""
    name:    str   # 逻辑名称，用于日志和状态文件 key
    src_dir: Path  # GRIB2 init 目录的父目录
    dst_dir: Path  # CSV 输出目录


def _parse_mapping_env(raw: str) -> list[SourceMapping]:
    """解析 GFS15M_MAPPING 环境变量为 SourceMapping 列表。"""
    result = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        parts = token.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"GFS15M_MAPPING 格式错误: '{token}'，期望 name:src:dst"
            )
        name, src, dst = parts
        src_path = Path(src) if Path(src).is_absolute() else ROOT / src
        dst_path = Path(dst) if Path(dst).is_absolute() else ROOT / dst
        result.append(SourceMapping(name.strip(), src_path, dst_path))
    return result


def _default_mappings() -> list[SourceMapping]:
    raw = os.getenv("GFS15M_MAPPING", "")
    if raw.strip():
        return _parse_mapping_env(raw)
    return [
        SourceMapping("jiangsu", ROOT / "data_js",  ROOT / "data_js_csv"),
        SourceMapping("sichuan", ROOT / "data_sc",  ROOT / "data_sc_csv"),
        SourceMapping("ningxia", ROOT / "data_nx",  ROOT / "data_nx_csv"),
    ]


# 运行时可被 main.py 中的 CLI 参数覆盖
MAPPINGS: list[SourceMapping] = _default_mappings()


# ---------------------------------------------------------------------------
# 调度参数（可被 env var 覆盖，再被 CLI 覆盖）
# ---------------------------------------------------------------------------

# 自动 CSV 转换当前默认停用，需要时显式开启
ENABLED: bool = os.getenv("GFS15M_ENABLED", "0") == "1"

# 定时全量扫描间隔（秒）
SCAN_INTERVAL_SEC: int = int(os.getenv("GFS15M_SCAN_INTERVAL", "300"))

# 并发 worker 数（同时处理的最大日期数）
MAX_WORKERS: int = int(os.getenv("GFS15M_WORKERS", "2"))

# 是否启用 watchdog 文件系统监听（需安装 watchdog 包）
WATCH_ENABLED: bool = os.getenv("GFS15M_NO_WATCH", "0") != "1"


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

LOG_DIR:          Path = Path(os.getenv("GFS15M_LOG_DIR", str(ROOT / "logs")))
LOG_FILE:         Path = LOG_DIR / "gfs_15min_guardian.log"
LOG_MAX_BYTES:    int  = 50 * 1024 * 1024   # 50 MB per file
LOG_BACKUP_COUNT: int  = 5


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

STATE_FILE: Path = Path(
    os.getenv("GFS15M_STATE_FILE", str(LOG_DIR / "gfs_15min_state.json"))
)
