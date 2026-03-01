#!/usr/bin/env python3
"""
gfs_15min_guardian — GRIB2 → 15 分钟 CSV 转换守护进程。

启动方式：
  python -m gfs_15min_guardian                     # 直接运行（默认配置）
  python -m gfs_15min_guardian --scan-interval 60  # 60 秒扫描一次
  systemctl start gfs-15min-guardian               # systemd 托管

线程模型：
  主线程  ── 阻塞在 stop_event.wait()，捕获 SIGTERM/SIGINT
  patrol  ── 定时全量扫描，发现待处理日期即提交任务
  watcher ── watchdog Observer（可选），目录创建事件驱动低延迟触发
  conv-*  ── ThreadPoolExecutor worker，执行实际 GRIB2→CSV 转换

配置优先级：环境变量 < CLI 参数
"""

import argparse
import logging
import logging.handlers
import signal
import sys
import threading
import time
from pathlib import Path

from . import state as _state
from .config import (
    LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT,
    SCAN_INTERVAL_SEC, MAX_WORKERS, WATCH_ENABLED,
    STATE_FILE, MAPPINGS, SourceMapping, _parse_mapping_env,
)
from .dispatcher import ConvTask, try_dispatch, shutdown as _shutdown_executor, init_executor
from .scanner import all_target_dates, needs_processing
from .watcher import start_watching


# ---------------------------------------------------------------------------
# 日志初始化
# ---------------------------------------------------------------------------

def _setup_logging(log_dir: Path, log_file: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-8s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    fh = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)
    root.addHandler(sh)


# ---------------------------------------------------------------------------
# 巡检循环
# ---------------------------------------------------------------------------

def _patrol_loop(
    mappings:     list[SourceMapping],
    shared_state: dict,
    state_file:   Path,
    scan_interval: int,
    stop_event:   threading.Event,
) -> None:
    """
    定时全量扫描循环。

    首次立即执行（启动时补扫历史目录），之后每 scan_interval 秒扫描一次。
    遇到单次异常记录日志后继续，保证服务不中断。
    """
    log = logging.getLogger("patrol")
    log.info("patrol started  scan_interval=%ds", scan_interval)

    while not stop_event.is_set():
        try:
            _do_scan(mappings, shared_state, state_file, log)
        except Exception as exc:
            log.error("scan loop error (will retry): %s", exc, exc_info=True)

        # 等待下次扫描，支持提前退出
        stop_event.wait(timeout=scan_interval)

    log.info("patrol stopped")


def _do_scan(
    mappings:     list[SourceMapping],
    shared_state: dict,
    state_file:   Path,
    log:          logging.Logger,
) -> None:
    """执行一轮全量扫描，提交所有待处理任务。"""
    dispatched = 0
    skipped    = 0

    for mapping in mappings:
        if not mapping.src_dir.is_dir():
            log.debug("src_dir 不存在，跳过: %s", mapping.src_dir)
            continue

        dates = all_target_dates(mapping)
        for date_str in (d.isoformat() for d in dates):
            task = ConvTask(mapping=mapping, date_str=date_str)
            if try_dispatch(task, shared_state, state_file):
                dispatched += 1
            else:
                skipped += 1

    log.info("scan done  dispatched=%d  skipped=%d", dispatched, skipped)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="GFS GRIB2 → 15 分钟 CSV 转换守护进程",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
环境变量（CLI 参数优先级更高）：
  GFS15M_SCAN_INTERVAL   扫描间隔（秒），默认 300
  GFS15M_WORKERS         并发 worker 数，默认 2
  GFS15M_NO_WATCH        设为 1 禁用 watchdog
  GFS15M_LOG_DIR         日志目录
  GFS15M_STATE_FILE      状态文件路径
  GFS15M_MAPPING         目录映射，格式: name:src:dst,...

目录映射默认值：
  jiangsu:data_js:data_js_csv
  sichuan:data_sc:data_sc_csv
  ningxia:data_nx:data_nx_csv
""",
    )
    p.add_argument(
        "--mapping", metavar="name:src:dst,...",
        help="目录映射（覆盖默认值及 GFS15M_MAPPING）",
    )
    p.add_argument(
        "--scan-interval", type=int, default=None, metavar="SEC",
        help=f"定时扫描间隔（秒），默认 {SCAN_INTERVAL_SEC}",
    )
    p.add_argument(
        "--workers", type=int, default=None, metavar="N",
        help=f"并发 worker 数，默认 {MAX_WORKERS}",
    )
    p.add_argument(
        "--no-watch", action="store_true", default=False,
        help="禁用 watchdog 文件系统监听，仅使用定时扫描",
    )
    p.add_argument(
        "--log-dir", default=None, metavar="DIR",
        help=f"日志目录，默认 {LOG_DIR}",
    )
    p.add_argument(
        "--state-file", default=None, metavar="FILE",
        help=f"状态文件路径，默认 {STATE_FILE}",
    )
    return p


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main() -> None:
    args = _build_parser().parse_args()

    # ── 应用 CLI 覆盖 ────────────────────────────────────────────────────────
    mappings      = _parse_mapping_env(args.mapping) if args.mapping else MAPPINGS
    scan_interval = args.scan_interval if args.scan_interval is not None else SCAN_INTERVAL_SEC
    watch_enabled = (not args.no_watch) and WATCH_ENABLED
    log_dir       = Path(args.log_dir)   if args.log_dir   else LOG_DIR
    log_file      = log_dir / "gfs_15min_guardian.log"
    state_file    = Path(args.state_file) if args.state_file else (
        log_dir / STATE_FILE.name
    )

    # workers 变更需重建 executor（此处简化：修改模块级常量后 executor 已用默认值创建）
    # 生产中如需动态 workers，可在此重建 executor

    # 初始化 executor（在日志和 dispatch 调用之前）
    init_executor(max_workers=args.workers)

    _setup_logging(log_dir, log_file)
    log = logging.getLogger("main")
    log.info("gfs_15min_guardian starting")
    log.info("mappings: %s",
             [(m.name, str(m.src_dir), str(m.dst_dir)) for m in mappings])
    log.info("scan_interval=%ds  watch=%s  workers=%s",
             scan_interval, watch_enabled, MAX_WORKERS if args.workers is None else args.workers)

    # ── 加载状态 ──────────────────────────────────────────────────────────────
    shared_state = _state.load(state_file)
    log.info("state loaded from %s", state_file)

    # ── 信号处理 ──────────────────────────────────────────────────────────────
    stop_event = threading.Event()

    def _on_signal(sig, _frame):
        log.info("收到信号 %s，正在优雅退出...", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)

    # ── watchdog 监听（可选）────────────────────────────────────────────────
    if watch_enabled:
        def _watcher_dispatch(mapping: SourceMapping, date_str: str) -> None:
            task = ConvTask(mapping=mapping, date_str=date_str)
            try_dispatch(task, shared_state, state_file)

        start_watching(mappings, _watcher_dispatch, stop_event)

    # ── 定时巡检线程 ─────────────────────────────────────────────────────────
    patrol_thread = threading.Thread(
        target=_patrol_loop,
        kwargs={
            "mappings":      mappings,
            "shared_state":  shared_state,
            "state_file":    state_file,
            "scan_interval": scan_interval,
            "stop_event":    stop_event,
        },
        name="patrol",
        daemon=True,
    )
    patrol_thread.start()
    log.info("所有线程已启动，等待退出信号")

    # ── 主线程阻塞 ───────────────────────────────────────────────────────────
    stop_event.wait()

    # ── 优雅退出 ─────────────────────────────────────────────────────────────
    log.info("等待正在运行的转换任务完成...")
    _shutdown_executor(wait=True)
    log.info("gfs_15min_guardian stopped")


if __name__ == "__main__":
    main()
