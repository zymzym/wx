"""
两条巡检循环：历史窗口（低频）和实时窗口（高频）。

两条循环各自运行在独立线程中，互不阻塞，通过 dispatcher.try_dispatch
共享同一个并发控制层（_in_flight + ThreadPoolExecutor）。

历史巡检：
  扫描 [region.start_date, yesterday]，仅 cycle=00，fh=0:23。
  发现缺失即触发下载；已完整则静默跳过。

实时巡检：
  从上游获取最近 8 个 date_cycle，对每个区域检查 fh=0:120 是否完整。
  发现缺失即触发下载。

重叠处理：
  若某 date/cycle 同时在历史窗口和实时窗口中，以实时窗口标准（fh=0:120）为准。
  实际效果：历史下载完成 fh=0:23 后，实时巡检发现 fh=0:120 不满足，
  再触发一次完整的 fh=0:120 下载（hist_fetch.py 覆盖写，幂等安全）。
"""

import logging
import time
from datetime import date, timedelta

from .checker import is_complete, missing_fh
from .config import (
    REGIONS,
    HIST_INTERVAL_SEC, RT_INTERVAL_SEC,
    HIST_CYCLE, HIST_FH_START, HIST_FH_END, HIST_FH_SPEC,
    RT_FH_START, RT_FH_END, RT_FH_SPEC,
)
from .dispatcher import Task, try_dispatch
from .realtime_source import LatestDateCyclesSource, fallback_latest_8

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _date_range(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    return [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]


# ---------------------------------------------------------------------------
# 历史巡检循环
# ---------------------------------------------------------------------------

def historical_patrol_loop() -> None:
    """
    持续循环，每 HIST_INTERVAL_SEC 秒扫描一次历史窗口。
    线程安全：只读文件系统 + dispatcher 内部加锁。
    """
    log.info("historical patrol started  interval=%ds", HIST_INTERVAL_SEC)

    while True:
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        dispatched = 0
        skipped    = 0

        for region in REGIONS:
            for d in _date_range(region.start_date, yesterday):
                if is_complete(region.out_dir, d, HIST_CYCLE,
                               HIST_FH_START, HIST_FH_END):
                    skipped += 1
                    continue

                missing = missing_fh(region.out_dir, d, HIST_CYCLE,
                                     HIST_FH_START, HIST_FH_END)
                log.debug("hist missing  %s/%s/%s  fh=%s",
                          region.name, d, HIST_CYCLE, missing[:5])

                task = Task(
                    region=region.name, date=d,
                    cycle=HIST_CYCLE,
                    fh_start=HIST_FH_START, fh_end=HIST_FH_END,
                    bbox=region.bbox, out_dir=region.out_dir,
                )
                if try_dispatch(task):
                    dispatched += 1

        log.info("historical scan done  dispatched=%d  already_ok=%d",
                 dispatched, skipped)

        time.sleep(HIST_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# 实时巡检循环
# ---------------------------------------------------------------------------

def realtime_patrol_loop(
    get_latest: LatestDateCyclesSource = fallback_latest_8,
) -> None:
    """
    持续循环，每 RT_INTERVAL_SEC 秒获取最近 8 个 date_cycle 并检查完整性。
    get_latest 可在 main.py 中替换为真实上游实现。
    """
    log.info("realtime patrol started  interval=%ds", RT_INTERVAL_SEC)

    while True:
        try:
            latest_8: list[tuple[str, str]] = get_latest()
        except Exception as exc:
            log.error("get_latest_date_cycles failed: %s — skip this round", exc)
            time.sleep(RT_INTERVAL_SEC)
            continue

        log.debug("realtime targets: %s", latest_8)

        dispatched = 0
        skipped    = 0

        for region in REGIONS:
            for (d, cycle) in latest_8:
                if is_complete(region.out_dir, d, cycle,
                               RT_FH_START, RT_FH_END):
                    skipped += 1
                    continue

                missing = missing_fh(region.out_dir, d, cycle,
                                     RT_FH_START, RT_FH_END)
                log.debug("rt missing  %s/%s/%s  fh=%s",
                          region.name, d, cycle, missing[:5])

                task = Task(
                    region=region.name, date=d, cycle=cycle,
                    fh_start=RT_FH_START, fh_end=RT_FH_END,
                    bbox=region.bbox, out_dir=region.out_dir,
                )
                if try_dispatch(task):
                    dispatched += 1

        log.info("realtime scan done  dispatched=%d  already_ok=%d",
                 dispatched, skipped)

        time.sleep(RT_INTERVAL_SEC)
