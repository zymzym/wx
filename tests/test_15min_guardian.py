"""
gfs_15min_guardian 单元测试。

测试范围：
  - state.py：状态文件读写、原子性
  - scanner.py：目标日期发现、幂等判定（needs_processing）
  - dispatcher.py：防抖（同一 key 只提交一次）

不依赖真实 GRIB2 数据，全部使用 tmp_path 临时目录。
"""

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path 准备（在 wx/ 目录之外运行测试时也能找到各模块）
# ---------------------------------------------------------------------------
_WX_ROOT = Path(__file__).resolve().parents[1]
if str(_WX_ROOT) not in sys.path:
    sys.path.insert(0, str(_WX_ROOT))

from gfs_15min_guardian import state as _state
from gfs_15min_guardian.config import SourceMapping
from gfs_15min_guardian.scanner import (
    all_target_dates,
    needs_processing,
    input_mtimes_for_date,
)
from gfs_15min_guardian.dispatcher import ConvTask, try_dispatch, _in_flight, _lock


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _make_init_dir(base: Path, name: str) -> Path:
    """在 base 下创建一个命名合法的 init 目录（如 20250901T0000Z）。"""
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_mapping(tmp_path: Path, name: str = "test") -> SourceMapping:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    return SourceMapping(name=name, src_dir=src, dst_dir=dst)


# ===========================================================================
# state.py 测试
# ===========================================================================

class TestState:
    def test_load_missing_file(self, tmp_path):
        """不存在的状态文件返回空 dict，不抛异常。"""
        s = _state.load(tmp_path / "nonexistent.json")
        assert s == {}

    def test_load_corrupt_file(self, tmp_path):
        """损坏的状态文件返回空 dict，不抛异常。"""
        f = tmp_path / "corrupt.json"
        f.write_text("NOT_JSON", encoding="utf-8")
        assert _state.load(f) == {}

    def test_save_and_load_roundtrip(self, tmp_path):
        """写入后读回内容一致。"""
        f = tmp_path / "state.json"
        data = {"jiangsu": {"2025-09-01": {"status": "done"}}}
        _state.save(f, data)
        loaded = _state.load(f)
        assert loaded == data

    def test_set_done_persists(self, tmp_path):
        """set_done 更新内存并持久化。"""
        f   = tmp_path / "state.json"
        st  = {}
        _state.set_done(st, f, "jiangsu", "2025-09-01",
                        duration_s=3.5,
                        input_mtimes={"20250901T0000Z": 1000.0})
        assert st["jiangsu"]["2025-09-01"]["status"] == "done"
        assert _state.load(f)["jiangsu"]["2025-09-01"]["status"] == "done"

    def test_set_failed_persists(self, tmp_path):
        """set_failed 更新内存并持久化。"""
        f  = tmp_path / "state.json"
        st = {}
        _state.set_failed(st, f, "jiangsu", "2025-09-01", error="boom")
        assert st["jiangsu"]["2025-09-01"]["status"] == "failed"
        assert _state.load(f)["jiangsu"]["2025-09-01"]["error"] == "boom"

    def test_atomic_write_uses_tmp(self, tmp_path, monkeypatch):
        """原子写入：save 先生成 .tmp 再 rename，不留临时文件。"""
        f  = tmp_path / "state.json"
        st = {"a": 1}
        _state.save(f, st)
        tmp_file = f.with_suffix(".json.tmp")
        assert not tmp_file.exists()
        assert f.exists()


# ===========================================================================
# scanner.py 测试
# ===========================================================================

class TestScanner:
    def test_all_target_dates_empty_dir(self, tmp_path):
        """空 src_dir 返回空列表。"""
        m = _make_mapping(tmp_path)
        assert all_target_dates(m) == []

    def test_all_target_dates_missing_dir(self, tmp_path):
        """src_dir 不存在返回空列表，不抛异常。"""
        m = SourceMapping("x", tmp_path / "no_such", tmp_path / "dst")
        assert all_target_dates(m) == []

    def test_all_target_dates_single_dir(self, tmp_path):
        """单个 init 目录 → 覆盖 24 小时对应的日期集合。"""
        m = _make_mapping(tmp_path)
        _make_init_dir(m.src_dir, "20250901T0000Z")
        dates = all_target_dates(m)
        assert len(dates) >= 1
        from datetime import date
        assert date(2025, 9, 1) in dates

    def test_all_target_dates_spans_two_days(self, tmp_path):
        """init 目录从 23:00Z 起，覆盖跨天两个日期。"""
        m = _make_mapping(tmp_path)
        _make_init_dir(m.src_dir, "20250901T2300Z")  # 23:00 to 22:00 next day
        dates = all_target_dates(m)
        from datetime import date
        assert date(2025, 9, 1) in dates
        assert date(2025, 9, 2) in dates

    def test_needs_processing_no_csv(self, tmp_path):
        """CSV 不存在 → 需要处理。"""
        m  = _make_mapping(tmp_path)
        st = {}
        assert needs_processing(m, "2025-09-01", st) is True

    def test_needs_processing_done_no_change(self, tmp_path):
        """CSV 存在 + state=done + mtime 不变 → 不需要处理。"""
        m = _make_mapping(tmp_path)
        # 创建 init 目录和 CSV
        init_dir = _make_init_dir(m.src_dir, "20250901T0000Z")
        csv = m.dst_dir / "gfs_15min_2025-09-01.csv"
        csv.write_text("header\n1,2,3", encoding="utf-8")
        # 读取实际 mtime
        mtimes = {init_dir.name: init_dir.stat().st_mtime}
        st = {
            "test": {
                "2025-09-01": {
                    "status": "done",
                    "input_mtimes": mtimes,
                }
            }
        }
        assert needs_processing(m, "2025-09-01", st) is False

    def test_needs_processing_failed_state(self, tmp_path):
        """CSV 存在但 state=failed → 需要重处理。"""
        m = _make_mapping(tmp_path)
        csv = m.dst_dir / "gfs_15min_2025-09-01.csv"
        csv.write_text("data", encoding="utf-8")
        st = {"test": {"2025-09-01": {"status": "failed"}}}
        assert needs_processing(m, "2025-09-01", st) is True

    def test_needs_processing_mtime_changed(self, tmp_path):
        """init 目录 mtime 变化 → 需要重处理。"""
        m        = _make_mapping(tmp_path)
        init_dir = _make_init_dir(m.src_dir, "20250901T0000Z")
        csv      = m.dst_dir / "gfs_15min_2025-09-01.csv"
        csv.write_text("data", encoding="utf-8")
        # 故意写入旧 mtime（与当前不同）
        st = {
            "test": {
                "2025-09-01": {
                    "status":       "done",
                    "input_mtimes": {init_dir.name: 0.0},  # 故意旧值
                }
            }
        }
        assert needs_processing(m, "2025-09-01", st) is True

    def test_input_mtimes_empty(self, tmp_path):
        """无 init 目录时返回空 dict。"""
        m = _make_mapping(tmp_path)
        assert input_mtimes_for_date(m, "2025-09-01") == {}

    def test_input_mtimes_returns_correct_dirs(self, tmp_path):
        """只返回覆盖目标日期的 init 目录。"""
        m = _make_mapping(tmp_path)
        _make_init_dir(m.src_dir, "20250901T0000Z")
        _make_init_dir(m.src_dir, "20250903T0000Z")  # 不覆盖 2025-09-01
        mtimes = input_mtimes_for_date(m, "2025-09-01")
        assert "20250901T0000Z" in mtimes
        assert "20250903T0000Z" not in mtimes


# ===========================================================================
# dispatcher.py 测试
# ===========================================================================

class TestDispatcher:
    def setup_method(self):
        """每个测试前清空 in_flight 集合，重置 executor。"""
        with _lock:
            _in_flight.clear()
        # 重置 executor（确保测试隔离）
        import gfs_15min_guardian.dispatcher as d
        with d._executor_lock:
            if d._executor is not None:
                d._executor.shutdown(wait=False)
                d._executor = None

    def test_dispatch_skipped_when_not_needed(self, tmp_path):
        """幂等：CSV 存在且 state=done → try_dispatch 返回 False。"""
        m   = _make_mapping(tmp_path)
        csv = m.dst_dir / "gfs_15min_2025-09-01.csv"
        csv.write_text("data")
        init_dir = _make_init_dir(m.src_dir, "20250901T0000Z")
        mtimes   = {init_dir.name: init_dir.stat().st_mtime}
        st       = {
            "test": {
                "2025-09-01": {
                    "status":       "done",
                    "input_mtimes": mtimes,
                }
            }
        }
        task = ConvTask(mapping=m, date_str="2025-09-01")
        result = try_dispatch(task, st, tmp_path / "state.json")
        assert result is False

    def test_dispatch_accepted_when_needed(self, tmp_path):
        """CSV 不存在 → try_dispatch 返回 True，任务进入 in_flight。"""
        m    = _make_mapping(tmp_path)
        st   = {}
        task = ConvTask(mapping=m, date_str="2025-09-01")

        # 替换 executor 为 mock，避免真实执行
        import gfs_15min_guardian.dispatcher as d
        results = []
        from concurrent.futures import ThreadPoolExecutor

        class MockExecutor:
            def submit(self, fn, *args, **kwargs):
                results.append(("submitted", args))
                return type("F", (), {"result": lambda self: None})()
            def shutdown(self, wait=True):
                pass

        with d._executor_lock:
            d._executor = MockExecutor()  # type: ignore

        result = try_dispatch(task, st, tmp_path / "state.json")
        assert result is True
        assert len(results) == 1

    def test_dispatch_dedup_same_key(self, tmp_path):
        """同一 (src_name, date_str) 第二次提交被跳过。"""
        m    = _make_mapping(tmp_path)
        st   = {}
        task = ConvTask(mapping=m, date_str="2025-09-01")

        import gfs_15min_guardian.dispatcher as d
        submitted = []

        class MockExecutor:
            def submit(self, fn, *args, **kwargs):
                submitted.append(1)
                return type("F", (), {"result": lambda self: None})()
            def shutdown(self, wait=True):
                pass

        with d._executor_lock:
            d._executor = MockExecutor()  # type: ignore

        r1 = try_dispatch(task, st, tmp_path / "state.json")
        r2 = try_dispatch(task, st, tmp_path / "state.json")

        assert r1 is True
        assert r2 is False        # 第二次因 in_flight 被拦截
        assert len(submitted) == 1

    def test_in_flight_cleared_after_run(self, tmp_path):
        """任务完成后，key 从 in_flight 中移除。"""
        m    = _make_mapping(tmp_path)
        st   = {}
        task = ConvTask(mapping=m, date_str="2025-09-01")

        import gfs_15min_guardian.dispatcher as d

        # 直接向 in_flight 加入 key，模拟任务运行中
        with _lock:
            _in_flight.add(task.key)

        # 调用 _run（会触发实际转换，但我们 mock convert 函数）
        convert_called = []

        def mock_convert(data_dir, target_dates, out_dir, verbose):
            # 创建 CSV 文件以满足后续检查
            out_dir.mkdir(parents=True, exist_ok=True)
            csv = out_dir / f"gfs_15min_{target_dates[0].isoformat()}.csv"
            csv.write_text("mocked")
            convert_called.append(1)

        import unittest.mock as mock
        with mock.patch("gfs_15min_guardian.dispatcher._do_convert") as mock_do:
            mock_do.side_effect = lambda task, st, sf, t0: None
            d._run(task, st, tmp_path / "state.json")

        # 任务完成后 key 应从 in_flight 中移除
        with _lock:
            assert task.key not in _in_flight
