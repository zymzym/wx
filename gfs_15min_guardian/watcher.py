"""
文件系统监听：watchdog 集成 + 纯轮询降级。

策略：
  - 优先使用 watchdog（低延迟，事件驱动）：若可用则启动 Observer 监听各 src_dir
  - 降级到纯轮询：若 watchdog 未安装，仅依赖 patrol.py 的定时扫描
  - 两者互补：即使 watchdog 正常工作，定时扫描仍作为兜底防止漏事件

watchdog 安装：
  pip install watchdog
"""

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import SourceMapping
from .scanner import dates_from_init_time, parse_dir_init_time

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# watchdog 可用性检测
# ---------------------------------------------------------------------------

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileCreatedEvent, DirCreatedEvent
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False


# ---------------------------------------------------------------------------
# DispatchCallback 类型
# ---------------------------------------------------------------------------

# 回调签名：(mapping: SourceMapping, date_str: str) -> None
DispatchCallback = Callable[[SourceMapping, str], None]


# ---------------------------------------------------------------------------
# watchdog 事件处理器
# ---------------------------------------------------------------------------

if _HAS_WATCHDOG:
    class _InitDirHandler(FileSystemEventHandler):
        """
        监听 src_dir 下的目录创建事件。
        当检测到格式合法的 init 目录（20YYMMDDTHHMMZ）出现时，
        立即为其覆盖的所有日期触发转换。
        """

        def __init__(self, mapping: SourceMapping, dispatch_cb: DispatchCallback):
            super().__init__()
            self._mapping     = mapping
            self._dispatch_cb = dispatch_cb

        def on_created(self, event) -> None:
            if not event.is_directory:
                return
            dirname   = Path(event.src_path).name
            init_time = parse_dir_init_time(dirname)
            if init_time is None:
                return
            log.debug("watcher detected new init dir: %s/%s", self._mapping.name, dirname)
            for date_str in dates_from_init_time(init_time):
                self._dispatch_cb(self._mapping, date_str)


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------

def start_watching(
    mappings:    list[SourceMapping],
    dispatch_cb: DispatchCallback,
    stop_event:  threading.Event,
) -> None:
    """
    启动文件系统监听（如 watchdog 可用）。

    本函数不阻塞：若启动了 Observer，会在后台线程运行；
    stop_event 置位时停止 Observer。

    参数：
      mappings    — 需要监听的源目录列表
      dispatch_cb — 回调：(mapping, date_str) -> None
      stop_event  — 主线程用于通知退出的事件
    """
    if not _HAS_WATCHDOG:
        log.info(
            "watchdog 未安装，使用纯轮询模式（可 pip install watchdog 启用低延迟事件检测）"
        )
        return

    observer = Observer()
    for m in mappings:
        m.src_dir.mkdir(parents=True, exist_ok=True)
        handler = _InitDirHandler(m, dispatch_cb)
        observer.schedule(handler, str(m.src_dir), recursive=False)

    observer.start()
    log.info("watchdog observer 已启动，监听 %d 个目录", len(mappings))

    # 在后台线程等待 stop_event，然后停止 observer
    def _stopper():
        stop_event.wait()
        observer.stop()
        observer.join()
        log.info("watchdog observer 已停止")

    threading.Thread(target=_stopper, name="watcher-stopper", daemon=True).start()
