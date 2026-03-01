#!/usr/bin/env python3
"""
gfs_guardian — 多区域 GFS 数据完整性守护进程。

启动方式：
  python -m gfs_guardian          # 直接运行
  systemctl start gfs-guardian    # systemd 托管

注入真实上游数据源（替换 fallback_latest_8）：
  在下方 # ── 注入点 ── 处替换 get_latest 实现即可，无需修改其他文件。
"""

import logging
import logging.handlers
import signal
import sys
import threading

from .config import LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT, REGIONS
from .patrol import historical_patrol_loop, realtime_patrol_loop
from .realtime_source import fallback_latest_8


# ---------------------------------------------------------------------------
# 日志初始化
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # 滚动文件日志
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 控制台日志（systemd journal 也会捕获）
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    _setup_logging()
    log = logging.getLogger("main")
    log.info("gfs_guardian starting")
    log.info("regions: %s", [r.name for r in REGIONS])

    # ── 注入点：替换为对接真实上游的实现 ──────────────────────────────────
    # 示例（HTTP 接口）：
    #   from myservice.client import fetch_latest_date_cycles as get_latest
    # 示例（数据库查询）：
    #   from myservice.db import query_latest_date_cycles as get_latest
    get_latest = fallback_latest_8
    # ─────────────────────────────────────────────────────────────────────

    stop_event = threading.Event()

    def _on_signal(sig, _frame):
        log.info("received signal %s — shutting down gracefully", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)

    # 历史巡检线程
    hist_thread = threading.Thread(
        target=historical_patrol_loop,
        name="hist-patrol",
        daemon=True,
    )

    # 实时巡检线程
    rt_thread = threading.Thread(
        target=realtime_patrol_loop,
        kwargs={"get_latest": get_latest},
        name="rt-patrol",
        daemon=True,
    )

    hist_thread.start()
    rt_thread.start()
    log.info("all patrol threads started")

    # 主线程阻塞等待退出信号
    stop_event.wait()
    log.info("gfs_guardian stopped")


if __name__ == "__main__":
    main()
