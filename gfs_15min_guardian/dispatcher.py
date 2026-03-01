"""
转换任务调度器：并发控制 + 防抖 + 幂等保证。

并发模型：
  - ThreadPoolExecutor(max_workers=MAX_WORKERS) 控制最大并发转换数
  - _in_flight set 跟踪"已提交但未完成"的任务 key，防止重复提交
  - 任务 key = (src_name, date_str)

幂等保证（双重检查）：
  1. try_dispatch() 入口：scanner.needs_processing() 快速路径（无锁）
  2. try_dispatch() 入口：_in_flight 检查（加锁）

异常隔离：
  - 单次失败只记录错误，不影响其他任务或服务存活
  - gfs_to_15min.run() 可能调用 sys.exit()，用 except SystemExit 捕获
"""

import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import MAX_WORKERS, SourceMapping
from .scanner import input_mtimes_for_date, needs_processing
from . import state as _state

log = logging.getLogger(__name__)

# 将项目根目录加入 sys.path，确保可以 import gfs_to_15min
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# 任务数据结构
# ---------------------------------------------------------------------------

@dataclass
class ConvTask:
    """代表一个"将某日期的 GRIB2 数据转换为 CSV"的任务。"""
    mapping:  SourceMapping
    date_str: str    # "YYYY-MM-DD"

    @property
    def key(self) -> tuple[str, str]:
        """唯一标识：(src_name, date_str)。"""
        return (self.mapping.name, self.date_str)

    @property
    def label(self) -> str:
        return f"{self.mapping.name}/{self.date_str}"


# ---------------------------------------------------------------------------
# 全局并发状态（模块级单例，executor 懒初始化支持 CLI 覆盖 workers 数）
# ---------------------------------------------------------------------------

_in_flight:      set[tuple[str, str]] = set()
_lock:           threading.Lock        = threading.Lock()
_executor:       ThreadPoolExecutor | None = None
_executor_lock:  threading.Lock        = threading.Lock()
_max_workers:    int                   = MAX_WORKERS


def init_executor(max_workers: int | None = None) -> None:
    """
    显式初始化 executor（在 main() 中调用，可覆盖默认 max_workers）。
    若已初始化则忽略。
    """
    global _executor, _max_workers
    with _executor_lock:
        if _executor is None:
            if max_workers is not None:
                _max_workers = max_workers
            _executor = ThreadPoolExecutor(
                max_workers=_max_workers,
                thread_name_prefix="conv",
            )


def _get_executor() -> ThreadPoolExecutor:
    """懒初始化：首次调用时用默认参数创建 executor。"""
    global _executor
    if _executor is None:
        init_executor()
    return _executor  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def try_dispatch(
    task:       ConvTask,
    shared_state: dict,
    state_file:   Path,
) -> bool:
    """
    尝试提交转换任务。

    返回 True 表示已提交；False 表示跳过（幂等/已在途）。

    双重检查：
      1. needs_processing()：快速幂等判定（无锁，只读）
      2. _in_flight 检查：防止同一目标并发处理（加锁）
    """
    if not needs_processing(task.mapping, task.date_str, shared_state):
        return False

    with _lock:
        if task.key in _in_flight:
            return False
        _in_flight.add(task.key)

    _get_executor().submit(_run, task, shared_state, state_file)
    log.info("dispatched  %s", task.label)
    return True


def shutdown(wait: bool = True) -> None:
    """优雅关闭 executor（等待正在运行的任务完成）。"""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=wait)
            _executor = None


# ---------------------------------------------------------------------------
# 内部执行
# ---------------------------------------------------------------------------

def _run(task: ConvTask, shared_state: dict, state_file: Path) -> None:
    """在工作线程中执行转换，完成后更新状态并释放 in_flight 槽。"""
    t0 = time.monotonic()
    log.info("converting  %s  src=%s  dst=%s",
             task.label, task.mapping.src_dir, task.mapping.dst_dir)
    try:
        _do_convert(task, shared_state, state_file, t0)
    finally:
        with _lock:
            _in_flight.discard(task.key)


def _do_convert(
    task:         ConvTask,
    shared_state: dict,
    state_file:   Path,
    t0:           float,
) -> None:
    """实际调用 gfs_to_15min.run() 并更新状态。"""
    # 快照输入目录 mtime（在转换开始前记录，确保一致性）
    mtimes = input_mtimes_for_date(task.mapping, task.date_str)

    try:
        # 延迟 import 避免循环依赖；gfs_to_15min 在 sys.path 中可找到
        from gfs_to_15min import run as _convert

        task.mapping.dst_dir.mkdir(parents=True, exist_ok=True)

        try:
            _convert(
                data_dir     = task.mapping.src_dir,
                target_dates = [date.fromisoformat(task.date_str)],
                out_dir      = task.mapping.dst_dir,
                verbose      = False,
            )
        except SystemExit as exc:
            # gfs_to_15min.run() 在无数据时调用 sys.exit()，守护层捕获并转换为普通异常
            raise RuntimeError(f"gfs_to_15min.run() exited: {exc}") from exc

        duration = time.monotonic() - t0
        _state.set_done(shared_state, state_file,
                        task.mapping.name, task.date_str,
                        duration, mtimes)
        log.info("done  %s  duration=%.1fs", task.label, duration)

    except Exception as exc:
        duration = time.monotonic() - t0
        _state.set_failed(shared_state, state_file,
                          task.mapping.name, task.date_str,
                          str(exc))
        log.error("failed  %s  duration=%.1fs  err=%s", task.label, duration, exc)
