#!/usr/bin/env python3
"""
hist_fetch.py — Batch historical GFS 0.25° downloader.

Data sources (automatic, in order):
  1. NOMADS filter_gfs_0p25.pl  — operational window (~10 days)
  2. AWS S3 noaa-gfs-bdp-pds    — full historical archive (fallback on 403/404)

All output (directory layout, file names, GRIB validation, manifest) is
identical to gfs_fetch.py. S3 files are always bbox-cropped with wgrib2
to match NOMADS output; S3 fallback fails if wgrib2 is unavailable.

Variables are fixed by the authoritative contract in gfs_repo/var_contract.py.

Usage:
  python hist_fetch.py \\
    --start 2026-01-01 --end 2026-01-31 \\
    --cycle 00 \\
    --bbox "112.25,31.5,122.0,34.5" \\
    --fh "0:23" \\
    --out ./gfs_data \\
    --retries 3 --timeout 30 \\
    --workers 4
"""

import sys
from pathlib import Path
# Make gfs_repo importable when running as a script from any working directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import io
import json
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from gfs_repo.var_contract import DEFAULT_VARS, VAR_MAPPING, to_nomads_lev

NOMADS_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
S3_BASE    = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
_HDRS      = {"User-Agent": "gfs_fetch/1.0"}
_LOCK      = threading.Lock()   # stdout lock for parallel mode


# ---------------------------------------------------------------------------
# wgrib2 detection (once at import time)
# ---------------------------------------------------------------------------
def _find_wgrib2() -> Optional[str]:
    configured = os.getenv("WGRIB2")
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path)
    for name in ("wgrib2", "wgrib2.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None

WGRIB2: Optional[str] = _find_wgrib2()
# True when wgrib2 is a Windows .exe invoked from WSL — paths need wslpath -w
_WGRIB2_IS_WIN_EXE: bool = bool(WGRIB2 and WGRIB2.endswith(".exe"))


def _require_wgrib2() -> str:
    """Return the crop executable or fail before saving uncropped S3 data."""
    if WGRIB2:
        return WGRIB2
    raise RuntimeError(
        "S3 fallback requires wgrib2 for bbox cropping, but it was not found. "
        "Install wgrib2 or set WGRIB2 to its executable path."
    )


def _wpath(p: Path) -> str:
    """Return a path string suitable for passing to wgrib2.

    When running wgrib2.exe from WSL, Linux paths must be converted to
    Windows paths (e.g. /tmp/foo → C:\\Users\\...\\AppData\\Local\\Temp\\foo).
    """
    if _WGRIB2_IS_WIN_EXE:
        try:
            r = subprocess.run(["wslpath", "-w", str(p)],
                               capture_output=True, text=True, check=True)
            return r.stdout.strip()
        except Exception:
            pass
    return str(p)


# ---------------------------------------------------------------------------
# NOMADS helpers
# ---------------------------------------------------------------------------

def _nomads_params(fname: str, cycle: str, date_str: str,
                   vars_: list[str], bbox: dict) -> dict:
    params: dict = {
        "file":      fname,
        "subregion": "on",
        "leftlon":   bbox["west"],
        "rightlon":  bbox["east"],
        "toplat":    bbox["north"],
        "bottomlat": bbox["south"],
        "dir":       f"/gfs.{date_str}/{cycle}/atmos",
    }
    seen_levs: set[str] = set()
    for v in vars_:
        entry = VAR_MAPPING[v]
        nom_lev = to_nomads_lev(entry["lev"])
        params[f"var_{entry['var']}"] = "on"
        if nom_lev not in seen_levs:
            params[f"lev_{nom_lev}"] = "on"
            seen_levs.add(nom_lev)
    return params


class _NomadsNoData(Exception):
    """Raised when NOMADS returns 403/404 — data not in operational window."""


def _nomads_once(params: dict, dest: Path, timeout: int) -> Optional[int]:
    """One NOMADS attempt.

    Returns file size on success (GRIB), None if response is not GRIB.
    Raises _NomadsNoData for HTTP 403/404.
    Raises requests.HTTPError for other 4xx/5xx.
    """
    resp = requests.get(NOMADS_URL, params=params, stream=True,
                        headers=_HDRS, timeout=timeout)
    if resp.status_code in (403, 404):
        raise _NomadsNoData(f"HTTP {resp.status_code}")
    resp.raise_for_status()

    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(65536):
            fh.write(chunk)

    with open(dest, "rb") as fh:
        if fh.read(4) == b"GRIB":
            return dest.stat().st_size
    return None   # HTML error page (200 OK but not GRIB)


# ---------------------------------------------------------------------------
# AWS S3 helpers
# ---------------------------------------------------------------------------

def _s3_url(date_str: str, cycle: str, fname: str) -> str:
    return f"{S3_BASE}/gfs.{date_str}/{cycle}/atmos/{fname}"


def _fetch_idx(date_str: str, cycle: str, fname: str,
               timeout: int) -> list[tuple[int, str]]:
    """Download .idx, return sorted [(byte_offset, raw_line), ...]."""
    url  = _s3_url(date_str, cycle, fname) + ".idx"
    resp = requests.get(url, headers=_HDRS, timeout=timeout)
    resp.raise_for_status()
    entries: list[tuple[int, str]] = []
    for line in resp.text.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 2:
            try:
                entries.append((int(parts[1]), line))
            except ValueError:
                pass
    entries.sort(key=lambda x: x[0])
    return entries


def _var_ranges(idx: list[tuple[int, str]],
                vars_: list[str]) -> list[tuple[int, Optional[int], str]]:
    """Return [(start_byte, end_byte_inclusive_or_None, var_key), ...].

    Matches the FIRST occurrence of each variable in the idx.
    end_byte is None for the last entry (read to EOF).
    The S3 .idx level field is matched as a substring, so partial strings
    like "entire atmosphere" match the full "entire atmosphere (considered
    as a single layer)" entries.
    """
    found_keys: set[str] = set()
    result: list[tuple[int, Optional[int], str]] = []
    for i, (offset, line) in enumerate(idx):
        for v in vars_:
            if v in found_keys:
                continue
            entry = VAR_MAPPING[v]
            if f":{entry['var']}:{entry['lev']}" in line:
                end = idx[i + 1][0] - 1 if i + 1 < len(idx) else None
                result.append((offset, end, v))
                found_keys.add(v)
                break
    return result


def _s3_download(fname: str, date_str: str, cycle: str, vars_: list[str],
                 bbox: dict, dest: Path, retries: int, timeout: int,
                 emit) -> int:
    """Download selected variables from S3 and crop them to bbox."""
    wgrib2 = _require_wgrib2()
    file_url = _s3_url(date_str, cycle, fname)

    for attempt in range(1, retries + 1):
        tmp = dest.with_suffix(".s3tmp")
        try:
            idx    = _fetch_idx(date_str, cycle, fname, timeout)
            ranges = _var_ranges(idx, vars_)

            if not ranges:
                raise RuntimeError(f"no matching variables found in S3 idx")

            missing = set(vars_) - {r[2] for r in ranges}
            if missing:
                emit(f"    [warn] S3: idx has no entry for: {', '.join(missing)}")

            # Download each variable's byte range and concatenate
            with open(tmp, "wb") as out:
                for start, end, _ in ranges:
                    rng  = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
                    r    = requests.get(file_url, timeout=timeout,
                                        headers={**_HDRS, "Range": rng})
                    r.raise_for_status()
                    out.write(r.content)

            # Validate GRIB magic
            with open(tmp, "rb") as fh:
                if fh.read(4) != b"GRIB":
                    raise RuntimeError("S3 data is not a valid GRIB2 file")

            lon_r = f"{bbox['west']}:{bbox['east']}"
            lat_r = f"{bbox['south']}:{bbox['north']}"
            proc = subprocess.run(
                [wgrib2, _wpath(tmp), "-small_grib",
                 lon_r, lat_r, _wpath(dest)],
                capture_output=True,
            )
            tmp.unlink(missing_ok=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or b"").decode(errors="replace")
                raise RuntimeError(
                    f"wgrib2 exited {proc.returncode}: {err.strip()}"
                )

            return dest.stat().st_size

        except Exception as exc:
            emit(f"    [!] S3: {exc} (attempt {attempt}/{retries})")
            dest.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))

    raise RuntimeError(f"Failed to download {fname} from S3 after {retries} attempts.")


# ---------------------------------------------------------------------------
# Unified download: NOMADS → S3 fallback
# ---------------------------------------------------------------------------

def download_one(fname: str, date_str: str, cycle: str, vars_: list[str],
                 bbox: dict, dest: Path, retries: int, timeout: int,
                 emit) -> int:
    """Try NOMADS filter; on 403/404 fall back to AWS S3.  Returns file size."""
    params  = _nomads_params(fname, cycle, date_str, vars_, bbox)
    use_s3  = False

    for attempt in range(1, retries + 1):
        try:
            size = _nomads_once(params, dest, timeout)
            if size is not None:
                return size
            emit(f"    [!] {fname}: NOMADS returned non-GRIB (attempt {attempt}/{retries})")

        except _NomadsNoData as e:
            emit(f"  [info] NOMADS {e} — data not in operational window, switching to S3")
            use_s3 = True
            break

        except Exception as exc:
            emit(f"    [!] {fname}: {exc} (attempt {attempt}/{retries})")

        dest.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))

    if use_s3:
        return _s3_download(fname, date_str, cycle, vars_, bbox,
                            dest, retries, timeout, emit)

    raise RuntimeError(f"Failed to download {fname} from NOMADS after {retries} attempts.")


# ---------------------------------------------------------------------------
# Single-date logic — identical behaviour to gfs_fetch.py
# ---------------------------------------------------------------------------

def fetch_one(date_: str, cycle: str, bbox: dict, out: Path,
              fh_start: int, fh_end: int,
              retries: int, timeout: int,
              emit=None) -> None:
    if emit is None:
        emit = print

    date_str  = date_.replace("-", "")
    init_time = f"{date_str}T{cycle}00Z"
    dest_dir  = out / init_time
    dest_dir.mkdir(parents=True, exist_ok=True)

    emit(f"init_time : {init_time}")
    emit(f"dest      : {dest_dir}")
    emit(f"fh        : {fh_start}–{fh_end}")
    emit(f"vars      : {', '.join(DEFAULT_VARS)}")
    if not WGRIB2:
        emit(f"  [warn] wgrib2 not found — S3 fallback will fail instead of saving uncropped data")
    emit("")

    files_meta: list[dict] = []

    for fh in range(fh_start, fh_end + 1):
        fname = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
        dest  = dest_dir / fname

        emit(f"  f{fh:03d}  {fname} … ", end="", flush=True)
        size = download_one(fname, date_str, cycle, DEFAULT_VARS, bbox,
                            dest, retries, timeout, emit)
        emit(f"{size:,} B")

        files_meta.append({"name": fname, "fh": fh, "size": size})

    manifest = {
        "init_time":      init_time,
        "date_utc":       date_str,
        "cycle":          cycle,
        "bbox":           bbox,
        "fh_range":       {"start": fh_start, "end": fh_end},
        "vars":           DEFAULT_VARS,
        "files":          files_meta,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = dest_dir / "manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    emit(f"\nmanifest  : {manifest_path}")
    emit(f"done      : {len(files_meta)} files")


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def _dates_in_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def run(start: date, end: date, cycle: str, bbox: dict, out: Path,
        fh_start: int, fh_end: int,
        retries: int, timeout: int, workers: int) -> None:

    dates  = _dates_in_range(start, end)
    total  = len(dates)
    kwargs = dict(cycle=cycle, bbox=bbox, out=out,
                  fh_start=fh_start, fh_end=fh_end,
                  retries=retries, timeout=timeout)

    print(f"[hist_fetch] {total} date(s)  {start} → {end}")
    print(f"  cycle={cycle}  fh={fh_start}:{fh_end}  vars={','.join(DEFAULT_VARS)}")
    print(f"  workers={workers}  retries={retries}  timeout={timeout}s")
    print(f"  wgrib2={WGRIB2 or '(not found)'}")
    print()

    failures: list[tuple[str, str]] = []

    # ── sequential ───────────────────────────────────────────────────────────
    if workers == 1:
        for i, d in enumerate(dates, 1):
            ds = d.strftime("%Y-%m-%d")
            print(f"[{i}/{total}] ── {ds} {'─' * 40}")
            try:
                fetch_one(ds, **kwargs)
            except Exception as exc:
                print(f"  [ERROR] {exc}", file=sys.stderr)
                failures.append((ds, str(exc)))
            print()

    # ── parallel ─────────────────────────────────────────────────────────────
    else:
        def _run(d: date):
            ds  = d.strftime("%Y-%m-%d")
            buf = io.StringIO()

            def emit(*args, file=None, end="\n", flush=False):
                buf.write(" ".join(str(a) for a in args) + end)

            exc_caught: Optional[Exception] = None
            try:
                fetch_one(ds, **kwargs, emit=emit)
            except Exception as exc:
                exc_caught = exc
            return ds, buf.getvalue(), exc_caught

        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map = {pool.submit(_run, d): d for d in dates}
            done = 0
            for fut in as_completed(fut_map):
                done += 1
                ds, output, exc = fut.result()
                if exc is None:
                    with _LOCK:
                        print(f"[{done}/{total}] ── {ds} {'─' * 40}")
                        print(output)
                else:
                    with _LOCK:
                        print(f"[{done}/{total}] ── {ds}  FAILED: {exc}",
                              file=sys.stderr)
                        for line in output.rstrip().splitlines():
                            print(f"  {line}", file=sys.stderr)
                    failures.append((ds, str(exc)))

    # ── summary ──────────────────────────────────────────────────────────────
    print("─" * 60)
    ok = total - len(failures)
    print(f"total={total}  ok={ok}  failed={len(failures)}")

    if failures:
        print(f"\nFailed ({len(failures)}):", file=sys.stderr)
        for ds, err in sorted(failures):
            print(f"  {ds}: {err}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Batch historical GFS 0.25° downloader (NOMADS + S3 fallback).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Downloads all {len(DEFAULT_VARS)} contract variables: "
               f"{', '.join(DEFAULT_VARS)}",
    )
    p.add_argument("--start",   required=True,
                   help="First UTC date, YYYY-MM-DD")
    p.add_argument("--end",     required=True,
                   help="Last UTC date, YYYY-MM-DD (inclusive)")
    p.add_argument("--cycle",   required=True, choices=["00", "06", "12", "18"],
                   help="Model cycle")
    p.add_argument("--bbox",    required=True,
                   help="west,south,east,north (degrees)")
    p.add_argument("--out",     default="./gfs_data",
                   help="Output root dir (default: ./gfs_data)")
    p.add_argument("--fh",      default="0:23",
                   help="Forecast-hour range start:end (default: 0:23)")
    p.add_argument("--retries", type=int, default=3,
                   help="Retries per file (default: 3)")
    p.add_argument("--timeout", type=int, default=30,
                   help="HTTP timeout in seconds (default: 30)")
    p.add_argument("--workers", type=int, default=1,
                   help="Parallel date workers (default: 1 = sequential)")
    args = p.parse_args()

    try:
        start = datetime.strptime(args.start, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"error: --start must be YYYY-MM-DD, got '{args.start}'")
    try:
        end = datetime.strptime(args.end, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"error: --end must be YYYY-MM-DD, got '{args.end}'")
    if start > end:
        sys.exit(f"error: --start {start} is after --end {end}")

    try:
        w, s, e, n = [float(x) for x in args.bbox.split(",")]
    except ValueError:
        sys.exit("error: --bbox must be 'west,south,east,north'")
    bbox = {"west": w, "south": s, "east": e, "north": n}

    try:
        fh_start, fh_end = [int(x) for x in args.fh.split(":")]
    except ValueError:
        sys.exit("error: --fh must be 'start:end', e.g. '0:23'")

    if args.workers < 1:
        sys.exit("error: --workers must be >= 1")

    run(
        start=start,
        end=end,
        cycle=args.cycle,
        bbox=bbox,
        out=Path(args.out),
        fh_start=fh_start,
        fh_end=fh_end,
        retries=args.retries,
        timeout=args.timeout,
        workers=args.workers,
    )


if __name__ == "__main__":
    main()
