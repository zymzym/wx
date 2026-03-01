#!/usr/bin/env python3
"""
端到端 Smoke 测试：gfs_15min_guardian 守护进程编排逻辑。

测试流程：
  1. 创建临时 src/dst 目录结构
  2. 注入 mock 转换函数（创建空 CSV，不读真实 GRIB2）
  3. 启动 patrol 循环短时间（约 3 秒）
  4. 验证：目标 CSV 出现，状态文件记录 done
  5. 新增 init 目录，等待再次扫描，验证新 CSV 出现
  6. 验证幂等：重复扫描不重复处理

用法：
  python tests/smoke_test_15min_guardian.py

依赖：无需真实 GRIB2 数据，无需外部网络。
"""

import os
import sys
import tempfile
import threading
import time
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# sys.path 准备
# ---------------------------------------------------------------------------
_WX_ROOT = Path(__file__).resolve().parents[1]
if str(_WX_ROOT) not in sys.path:
    sys.path.insert(0, str(_WX_ROOT))

# ---------------------------------------------------------------------------
# 测试辅助
# ---------------------------------------------------------------------------

def _make_init_dir(src_dir: Path, name: str) -> Path:
    d = src_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _mock_convert(data_dir: Path, target_dates: list, out_dir: Path, verbose: bool) -> None:
    """替代真实 gfs_to_15min.run() 的 mock 实现：直接创建空 CSV 文件。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for d in target_dates:
        csv = out_dir / f"gfs_15min_{d.isoformat()}.csv"
        csv.write_text(f"mocked,{d}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 主测试
# ---------------------------------------------------------------------------

def run_smoke_test():
    print("=" * 60)
    print("gfs_15min_guardian Smoke Test")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="gfs15m_smoke_") as tmpdir:
        tmp = Path(tmpdir)
        src = tmp / "data_test"
        dst = tmp / "data_test_csv"
        src.mkdir()
        dst.mkdir()
        state_file = tmp / "state.json"
        log_dir    = tmp / "logs"

        from gfs_15min_guardian.config import SourceMapping
        from gfs_15min_guardian import state as _state
        from gfs_15min_guardian.dispatcher import (
            ConvTask, try_dispatch, _in_flight, _lock, init_executor,
        )
        import gfs_15min_guardian.dispatcher as d

        # Reset dispatcher state
        with _lock:
            _in_flight.clear()
        with d._executor_lock:
            if d._executor is not None:
                d._executor.shutdown(wait=False)
                d._executor = None
        init_executor(max_workers=1)

        mapping = SourceMapping(name="smoke", src_dir=src, dst_dir=dst)

        # ── STEP 1: 写入初始 init 目录 ──────────────────────────────────
        print("\n[1] 创建初始 init 目录: 20250901T0000Z, 20250902T0000Z")
        _make_init_dir(src, "20250901T0000Z")
        _make_init_dir(src, "20250902T0000Z")

        shared_state = _state.load(state_file)
        stop_event   = threading.Event()

        # ── STEP 2: Monkey-patch convert 函数 ────────────────────────────
        # 注入 mock，避免调用真实 GRIB2 处理
        import unittest.mock as mock
        convert_calls = []

        def _patched_do_convert(task, st, sf, t0):
            import time as _t
            _mock_convert(task.mapping.src_dir,
                          [date.fromisoformat(task.date_str)],
                          task.mapping.dst_dir,
                          verbose=False)
            convert_calls.append(task.date_str)
            duration = _t.monotonic() - t0
            mtimes_snap = {}  # 简化：smoke test 不做 mtime 检查
            _state.set_done(st, sf, task.mapping.name, task.date_str, duration, mtimes_snap)

        # ── STEP 3: 运行 patrol 循环（scan_interval=1s，运行 3s）──────────
        print("[2] 启动 patrol 线程（scan_interval=1s）")

        from gfs_15min_guardian.scanner import all_target_dates, needs_processing
        from gfs_15min_guardian.dispatcher import _run

        def _patrol():
            from gfs_15min_guardian.scanner import all_target_dates
            while not stop_event.is_set():
                for date_obj in all_target_dates(mapping):
                    task = ConvTask(mapping=mapping, date_str=date_obj.isoformat())
                    try_dispatch(task, shared_state, state_file)
                stop_event.wait(timeout=1.0)

        with mock.patch("gfs_15min_guardian.dispatcher._do_convert",
                        side_effect=_patched_do_convert):
            patrol = threading.Thread(target=_patrol, daemon=True)
            patrol.start()
            time.sleep(3.0)

        # ── STEP 4: 验证初始输出 ─────────────────────────────────────────
        print("\n[3] 验证 CSV 输出...")
        csv_01 = dst / "gfs_15min_2025-09-01.csv"
        csv_02 = dst / "gfs_15min_2025-09-02.csv"
        assert csv_01.exists(), f"FAIL: {csv_01} 不存在"
        assert csv_02.exists(), f"FAIL: {csv_02} 不存在"
        print(f"    OK: {csv_01.name}")
        print(f"    OK: {csv_02.name}")

        # ── STEP 5: 验证状态文件 ─────────────────────────────────────────
        print("[4] 验证状态文件...")
        st_loaded = _state.load(state_file)
        assert st_loaded.get("smoke", {}).get("2025-09-01", {}).get("status") == "done"
        assert st_loaded.get("smoke", {}).get("2025-09-02", {}).get("status") == "done"
        print("    OK: 2025-09-01 → done")
        print("    OK: 2025-09-02 → done")

        # ── STEP 6: 验证幂等（重置并重新扫描不重新处理）──────────────────
        print("[5] 验证幂等（重新扫描不重处理）...")
        initial_count = len(convert_calls)

        with mock.patch("gfs_15min_guardian.dispatcher._do_convert",
                        side_effect=_patched_do_convert):
            # 重新加载状态，再扫描一轮
            shared_state2 = _state.load(state_file)
            stop2 = threading.Event()

            def _patrol2():
                for date_obj in all_target_dates(mapping):
                    task = ConvTask(mapping=mapping, date_str=date_obj.isoformat())
                    # needs_processing 应为 False（mtime 未变，CSV 存在）
                    # 但 smoke test 中 input_mtimes 为空 {}，所以不能完全验证 mtime 路径
                    # 这里验证的是 state=done 路径
                    try_dispatch(task, shared_state2, state_file)
                stop2.set()

            t2 = threading.Thread(target=_patrol2, daemon=True)
            t2.start()
            t2.join(timeout=3)

        new_count = len(convert_calls)
        # 由于 smoke test 中 input_mtimes={} 与 saved mtimes={} 相同，幂等生效
        assert new_count == initial_count, (
            f"FAIL: 幂等失败，重新扫描触发了 {new_count - initial_count} 次额外转换"
        )
        print(f"    OK: 无额外转换（total calls={new_count}）")

        # ── STEP 7: 新增 init 目录，验证被发现并触发 ──────────────────────
        print("[6] 新增 init 目录 20250903T0000Z，验证被发现...")
        _make_init_dir(src, "20250903T0000Z")

        with mock.patch("gfs_15min_guardian.dispatcher._do_convert",
                        side_effect=_patched_do_convert):
            # 重置 in_flight（模拟新的服务周期）
            with _lock:
                _in_flight.clear()
            # 加载最新状态，触发扫描
            shared_state3 = _state.load(state_file)
            stop3 = threading.Event()

            def _patrol3():
                for date_obj in all_target_dates(mapping):
                    task = ConvTask(mapping=mapping, date_str=date_obj.isoformat())
                    try_dispatch(task, shared_state3, state_file)
                stop3.set()

            t3 = threading.Thread(target=_patrol3, daemon=True)
            t3.start()
            time.sleep(2.0)  # 等待任务执行

        csv_03 = dst / "gfs_15min_2025-09-03.csv"
        assert csv_03.exists(), f"FAIL: {csv_03} 不存在"
        print(f"    OK: {csv_03.name}")

        # ── 停止 patrol ───────────────────────────────────────────────────
        stop_event.set()

    print("\n" + "=" * 60)
    print("Smoke Test PASSED ✓")
    print("=" * 60)


if __name__ == "__main__":
    run_smoke_test()
