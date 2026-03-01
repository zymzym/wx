#!/usr/bin/env python3
"""
compare_interfaces.py — 下载当前（NOMADS）与历史（S3）各一个 GRIB 文件，
                        用 wgrib2 列出字段集合，验证两个接口输出变量一致。

Usage:
  python3 compare_interfaces.py \
    --recent  2026-02-24 \
    --hist    2026-01-15 \
    --cycle   00 --fh 3 \
    --bbox    "119,30,122,33"
"""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from gfs_repo.var_contract import DEFAULT_VARS, VAR_MAPPING, to_nomads_lev

# ── ANSI ─────────────────────────────────────────────────────────────────────
G = "\033[32m"; R = "\033[31m"; Y = "\033[33m"; B = "\033[1m"; E = "\033[0m"

NOMADS_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
S3_BASE    = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
_HDRS      = {"User-Agent": "gfs_fetch/1.0"}


# ── wgrib2 ────────────────────────────────────────────────────────────────────

def _find_wgrib2() -> str:
    for name in ("wgrib2", "wgrib2.exe"):
        p = shutil.which(name)
        if p:
            return p
    sys.exit(f"{R}wgrib2 not found in PATH — cannot list GRIB fields{E}")

WGRIB2 = _find_wgrib2()
_IS_WIN = WGRIB2.endswith(".exe")


def _wpath(p: Path) -> str:
    if _IS_WIN:
        r = subprocess.run(["wslpath", "-w", str(p)],
                           capture_output=True, text=True, check=True)
        return r.stdout.strip()
    return str(p)


def grib_fields(path: Path) -> list[str]:
    """Return sorted list of 'VAR:level' strings from a GRIB2 file."""
    proc = subprocess.run(
        [WGRIB2, _wpath(path)],
        capture_output=True, text=True,
    )
    fields = []
    for line in proc.stdout.strip().splitlines():
        # format:  N:offset:d=...:VAR:level:fcst:
        parts = line.split(":")
        if len(parts) >= 5:
            fields.append(f"{parts[3]}:{parts[4]}")
    return sorted(set(fields))


# ── Download helpers ──────────────────────────────────────────────────────────

def _nomads_params(fname: str, cycle: str, date_str: str, bbox: dict) -> dict:
    params: dict = {
        "file": fname, "subregion": "on",
        "leftlon": bbox["w"], "rightlon": bbox["e"],
        "toplat":  bbox["n"], "bottomlat": bbox["s"],
        "dir": f"/gfs.{date_str}/{cycle}/atmos",
    }
    seen: set[str] = set()
    for v in DEFAULT_VARS:
        entry = VAR_MAPPING[v]
        nom_lev = to_nomads_lev(entry["lev"])
        params[f"var_{entry['var']}"] = "on"
        if nom_lev not in seen:
            params[f"lev_{nom_lev}"] = "on"
            seen.add(nom_lev)
    return params


def download_nomads(date: str, cycle: str, fh: int, bbox: dict, dest: Path) -> None:
    date_str = date.replace("-", "")
    fname    = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
    params   = _nomads_params(fname, cycle, date_str, bbox)
    print(f"  GET NOMADS  {date} f{fh:03d} …", end="", flush=True)
    resp = requests.get(NOMADS_URL, params=params, stream=True,
                        headers=_HDRS, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(65536):
            f.write(chunk)
    with open(dest, "rb") as f:
        if f.read(4) != b"GRIB":
            raise RuntimeError("NOMADS returned non-GRIB response")
    print(f"  {dest.stat().st_size:,} B")


def download_s3(date: str, cycle: str, fh: int, bbox: dict, dest: Path) -> None:
    """S3 byte-range download (same logic as hist_fetch.py)."""
    date_str = date.replace("-", "")
    fname    = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
    base_url = f"{S3_BASE}/gfs.{date_str}/{cycle}/atmos/{fname}"

    # Fetch idx
    print(f"  GET S3 idx  {date} f{fh:03d} …", end="", flush=True)
    idx_resp = requests.get(base_url + ".idx", headers=_HDRS, timeout=30)
    idx_resp.raise_for_status()
    entries: list[tuple[int, str]] = []
    for line in idx_resp.text.strip().splitlines():
        parts = line.split(":")
        if len(parts) >= 2:
            try:
                entries.append((int(parts[1]), line))
            except ValueError:
                pass
    entries.sort(key=lambda x: x[0])
    print(f"  {len(entries)} idx entries")

    # Match variables
    found_keys: set[str] = set()
    ranges: list[tuple[int, int | None]] = []
    for i, (offset, line) in enumerate(entries):
        for v in DEFAULT_VARS:
            if v in found_keys:
                continue
            entry = VAR_MAPPING[v]
            if f":{entry['var']}:{entry['lev']}" in line:
                end = entries[i + 1][0] - 1 if i + 1 < len(entries) else None
                ranges.append((offset, end))
                found_keys.add(v)
                break

    missing = set(DEFAULT_VARS) - found_keys
    if missing:
        print(f"  {Y}[warn] not found in S3 idx: {', '.join(missing)}{E}")

    # Download byte ranges into tmp
    tmp = dest.with_suffix(".s3tmp")
    print(f"  GET S3 data {len(ranges)} ranges …", end="", flush=True)
    with open(tmp, "wb") as out:
        for start, end in ranges:
            rng = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
            r   = requests.get(base_url, headers={**_HDRS, "Range": rng}, timeout=60)
            r.raise_for_status()
            out.write(r.content)

    with open(tmp, "rb") as f:
        if f.read(4) != b"GRIB":
            tmp.unlink(missing_ok=True)
            raise RuntimeError("S3 byte-range response is not valid GRIB2")

    # Crop with wgrib2
    lon_r = f"{bbox['w']}:{bbox['e']}"
    lat_r = f"{bbox['s']}:{bbox['n']}"
    proc  = subprocess.run(
        [WGRIB2, _wpath(tmp), "-small_grib", lon_r, lat_r, _wpath(dest)],
        capture_output=True,
    )
    tmp.unlink(missing_ok=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode(errors="replace")
        raise RuntimeError(f"wgrib2 crop failed: {err.strip()}")
    print(f"  {dest.stat().st_size:,} B")


# ── Comparison ────────────────────────────────────────────────────────────────

def compare(fields_nomads: list[str], fields_s3: list[str],
            label_nomads: str, label_s3: str) -> bool:
    """
    成功条件：S3 的所有字段（= 精确的契约字段集）在 NOMADS 里也存在。
    NOMADS 多出的字段是 filter 的结构性副产品（level bleed），视为 INFO。
    """
    set_n, set_s = set(fields_nomads), set(fields_s3)
    common        = sorted(set_n & set_s)          # 两侧都有
    nomads_extra  = sorted(set_n - set_s)          # NOMADS 多余（level bleed）
    s3_missing_in_nomads = sorted(set_s - set_n)   # S3 有、NOMADS 没有 → 真正缺失

    print(f"\n{'─'*60}")
    print(f"  {B}契约变量（共同字段）{E}: {G}{len(common)}{E} / {len(DEFAULT_VARS)}")

    # ── NOMADS 结构性附带字段 ──────────────────────────────────────────────
    if nomads_extra:
        print(f"\n  {Y}[INFO] NOMADS level-bleed 附带字段 ({len(nomads_extra)}):{E}")
        print(f"  {Y}  原因：NOMADS filter 以 level 为粒度，"
              f"请求某压层时返回该层全部变量{E}")
        for f in nomads_extra:
            print(f"    · {f}")

    # ── S3 有、NOMADS 没有 → 契约缺失 ───────────────────────────────────
    if s3_missing_in_nomads:
        print(f"\n  {R}[ERROR] 契约变量在 NOMADS 中缺失 ({len(s3_missing_in_nomads)}):{E}")
        for f in s3_missing_in_nomads:
            print(f"    {R}✗ {f}{E}")
        return False

    # ── 全部通过 ─────────────────────────────────────────────────────────
    print(f"\n  {G}✓ 所有 {len(common)} 个契约变量在两个接口中均存在{E}")
    print(f"\n  契约字段列表:")
    for f in common:
        print(f"    {G}✓{E} {f}")
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--recent", default="2026-02-24",
                   help="Recent date (NOMADS), YYYY-MM-DD")
    p.add_argument("--hist",   default="2026-01-15",
                   help="Historical date (S3),   YYYY-MM-DD")
    p.add_argument("--cycle",  default="00", choices=["00","06","12","18"])
    p.add_argument("--fh",     type=int, default=3,
                   help="Forecast hour (default 3; ≥1 required for accumulated fields)")
    p.add_argument("--bbox",   default="119,30,122,33",
                   help="west,south,east,north (default: 119,30,122,33)")
    args = p.parse_args()

    w, s, e, n = [float(x) for x in args.bbox.split(",")]
    bbox = {"w": w, "s": s, "e": e, "n": n}

    print(f"\n{B}=== GFS 双接口字段一致性对比 ==={E}")
    print(f"  当前接口 (NOMADS): {args.recent}  cycle={args.cycle}  f{args.fh:03d}")
    print(f"  历史接口 (S3    ): {args.hist}   cycle={args.cycle}  f{args.fh:03d}")
    print(f"  bbox: W={w} S={s} E={e} N={n}")
    print(f"  变量契约: {len(DEFAULT_VARS)} vars\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        file_nomads = tmp / "nomads.grb2"
        file_s3     = tmp / "s3.grb2"

        # ── Download ──────────────────────────────────────────────────────────
        print(f"{B}[1/2] 当前接口 → NOMADS{E}")
        try:
            download_nomads(args.recent, args.cycle, args.fh, bbox, file_nomads)
        except Exception as exc:
            sys.exit(f"{R}NOMADS 下载失败: {exc}{E}")

        print(f"\n{B}[2/2] 历史接口 → S3{E}")
        try:
            download_s3(args.hist, args.cycle, args.fh, bbox, file_s3)
        except Exception as exc:
            sys.exit(f"{R}S3 下载失败: {exc}{E}")

        # ── List fields ───────────────────────────────────────────────────────
        print(f"\n{B}[wgrib2] 解析字段列表{E}")
        fields_nomads = grib_fields(file_nomads)
        fields_s3     = grib_fields(file_s3)
        print(f"  NOMADS 字段数: {len(fields_nomads)}")
        print(f"  S3     字段数: {len(fields_s3)}")

        # ── Compare ───────────────────────────────────────────────────────────
        ok = compare(fields_nomads, fields_s3,
                     f"NOMADS({args.recent})", f"S3({args.hist})")  # noqa: E501

    print(f"\n{'─'*60}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
