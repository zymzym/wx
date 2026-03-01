"""
下载调度器：subprocess 执行 + 并发控制 + 幂等保证 + 重试。

并发模型：
  - ThreadPoolExecutor(max_workers=MAX_CONCURRENT) 控制最大并发下载数
  - _in_flight set 跟踪"已提交但未完成"的任务 key，防止重复触发
  - 重试通过定时线程延迟后重新提交（不阻塞巡检线程）
"""

import logging
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .checker import is_complete
from .config import (
    MAX_CONCURRENT, MAX_RETRIES, RETRY_BACKOFF_SEC,
    ROOT, TASK_LOG_DIR,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Task 数据结构
# ---------------------------------------------------------------------------

@dataclass
class Task:
    region:   str
    date:     str       # "YYYY-MM-DD"
    cycle:    str       # "00" | "06" | "12" | "18"
    fh_start: int
    fh_end:   int
    bbox:     str
    out_dir:  str       # 相对于 ROOT
    attempt:  int = field(default=0)

    @property
    def key(self) -> tuple[str, str, str]:
        """唯一标识一个下载目标（与 fh 范围无关）。"""
        return (self.region, self.date, self.cycle)

    @property
    def label(self) -> str:
        return f"{self.region}/{self.date}/{self.cycle}"


# ---------------------------------------------------------------------------
# 全局并发状态（模块级单例）
# ---------------------------------------------------------------------------

_in_flight: set[tuple[str, str, str]] = set()
_lock:      threading.Lock            = threading.Lock()
_executor:  ThreadPoolExecutor        = ThreadPoolExecutor(
    max_workers=MAX_CONCURRENT,
    thread_name_prefix="dl",
)


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def try_dispatch(task: Task) -> bool:
    """
    尝试提交任务。返回 True 表示已提交，False 表示跳过（完整/已在途）。

    幂等保证：
      1. 先做完整性检查，已完整直接跳过
      2. 检查 _in_flight，已在途直接跳过
      3. 两步均通过才加入 _in_flight 并提交
    """
    # 快速路径：完整性检查（无锁，只读文件系统）
    if is_complete(task.out_dir, task.date, task.cycle, task.fh_start, task.fh_end):
        return False

    with _lock:
        if task.key in _in_flight:
            return False
        _in_flight.add(task.key)

    _executor.submit(_run, task)
    log.info("dispatched  %s  fh=%d:%d  attempt=%d",
             task.label, task.fh_start, task.fh_end, task.attempt)
    return True


# ---------------------------------------------------------------------------
# 内部执行
# ---------------------------------------------------------------------------

def _run(task: Task) -> None:
    """在工作线程中执行，完成后从 _in_flight 移除。"""
    try:
        _download(task)
        log.info("done  %s", task.label)
    except Exception as exc:
        log.error("failed  %s  attempt=%d  err=%s", task.label, task.attempt, exc)
        _schedule_retry(task)
    finally:
        with _lock:
            _in_flight.discard(task.key)


def _download(task: Task) -> None:
    """调用 hist_fetch.py，stdout/stderr 写入任务日志文件。"""
    TASK_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = TASK_LOG_DIR / f"{task.region}_{task.date}_{task.cycle}_a{task.attempt}.log"

    cmd = [
        sys.executable,
        str(ROOT / "hist_fetch" / "hist_fetch.py"),
        "--start",  task.date,
        "--end",    task.date,
        "--cycle",  task.cycle,
        "--bbox",   task.bbox,
        "--fh",     f"{task.fh_start}:{task.fh_end}",
        "--out",    str(ROOT / task.out_dir),
    ]

    log.debug("CMD: %s  log=%s", " ".join(cmd), log_path)

    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"=== {task.label}  attempt={task.attempt} ===\n")
        lf.write(f"CMD: {' '.join(cmd)}\n\n")
        lf.flush()
        proc = subprocess.run(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
        )

    if proc.returncode != 0:
        # 读取日志尾部作为错误摘要
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail  = "\n".join(lines[-15:])
        raise RuntimeError(f"exit={proc.returncode}\n{tail}")


def _schedule_retry(task: Task) -> None:
    """在后台线程中等待退避时间，然后重新提交任务。"""
    if task.attempt >= MAX_RETRIES:
        log.error("give up  %s  (max_retries=%d reached)", task.label, MAX_RETRIES)
        return

    delay     = RETRY_BACKOFF_SEC[min(task.attempt, len(RETRY_BACKOFF_SEC) - 1)]
    next_task = Task(
        region=task.region, date=task.date, cycle=task.cycle,
        fh_start=task.fh_start, fh_end=task.fh_end,
        bbox=task.bbox, out_dir=task.out_dir,
        attempt=task.attempt + 1,
    )

    log.info("retry #%d scheduled in %ds  %s", next_task.attempt, delay, task.label)

    def _deferred():
        time.sleep(delay)
        # 重试前再做一次完整性检查，避免期间已被其他路径补齐
        if is_complete(task.out_dir, task.date, task.cycle, task.fh_start, task.fh_end):
            log.info("retry skipped (already complete)  %s", task.label)
            return
        with _lock:
            if next_task.key in _in_flight:
                return
            _in_flight.add(next_task.key)
        _executor.submit(_run, next_task)
        log.info("retry dispatched  %s  attempt=%d", task.label, next_task.attempt)

    threading.Thread(
        target=_deferred,
        daemon=True,
        name=f"retry-{task.region}-{task.date}-{task.cycle}",
    ).start()
