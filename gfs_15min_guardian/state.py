"""
线程安全的 JSON 状态持久化。

状态文件结构：
{
  "jiangsu": {
    "2025-09-01": {
      "status":       "done" | "failed",
      "finished_at":  "2025-09-01T12:00:00+00:00",
      "duration_s":   5.3,
      "input_mtimes": {"20250901T0000Z": 1700000000.0, ...},
      "error":        "..." (仅 failed 时存在)
    }
  }
}

线程安全策略：
  - 内存中维护一份 dict，由 _lock (RLock) 保护
  - 写入磁盘时先写 .tmp 文件再原子 rename，防止进程崩溃时损坏状态文件
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_lock: threading.RLock = threading.RLock()


# ---------------------------------------------------------------------------
# 加载 / 保存
# ---------------------------------------------------------------------------

def load(state_file: Path) -> dict:
    """从磁盘加载状态，失败时返回空 dict（不抛出异常）。"""
    if not state_file.exists():
        return {}
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(state_file: Path, data: dict) -> None:
    """原子写入状态文件。写 .tmp 后 rename，保证不损坏已有文件。"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".json.tmp")
    with _lock:
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(tmp, state_file)


# ---------------------------------------------------------------------------
# 细粒度读写（持有锁期间操作内存 dict）
# ---------------------------------------------------------------------------

def get_entry(state: dict, src_name: str, date_str: str) -> dict | None:
    """返回某 (src_name, date_str) 的状态条目，不存在返回 None。"""
    with _lock:
        return state.get(src_name, {}).get(date_str)


def set_done(
    state: dict,
    state_file: Path,
    src_name: str,
    date_str: str,
    duration_s: float,
    input_mtimes: dict[str, float],
) -> None:
    """标记某日期转换成功，并持久化。"""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        state.setdefault(src_name, {})[date_str] = {
            "status":       "done",
            "finished_at":  now,
            "duration_s":   round(duration_s, 2),
            "input_mtimes": input_mtimes,
        }
        save(state_file, state)


def set_failed(
    state: dict,
    state_file: Path,
    src_name: str,
    date_str: str,
    error: str,
) -> None:
    """标记某日期转换失败，并持久化。"""
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        state.setdefault(src_name, {})[date_str] = {
            "status":      "failed",
            "finished_at": now,
            "error":       error[:500],   # 截断超长错误信息
        }
        save(state_file, state)
