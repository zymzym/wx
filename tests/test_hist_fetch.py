from pathlib import Path

import pytest

from hist_fetch import hist_fetch


def test_s3_download_requires_wgrib2_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr(hist_fetch, "WGRIB2", None)

    def unexpected_network(*args, **kwargs):
        raise AssertionError("network must not be called without wgrib2")

    monkeypatch.setattr(hist_fetch, "_fetch_idx", unexpected_network)

    with pytest.raises(RuntimeError, match="requires wgrib2 for bbox cropping"):
        hist_fetch._s3_download(
            fname="gfs.t00z.pgrb2.0p25.f000",
            date_str="20250101",
            cycle="00",
            vars_=["t2m"],
            bbox={"west": 100, "south": 20, "east": 110, "north": 30},
            dest=tmp_path / "out.grib2",
            retries=1,
            timeout=1,
            emit=lambda *args, **kwargs: None,
        )


def test_find_wgrib2_accepts_explicit_environment_path(tmp_path, monkeypatch):
    executable = tmp_path / "wgrib2"
    executable.write_bytes(b"")
    monkeypatch.setenv("WGRIB2", str(executable))

    assert hist_fetch._find_wgrib2() == str(executable)
