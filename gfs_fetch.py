#!/usr/bin/env python3
"""
gfs_fetch.py — Stateless GFS 0.25° downloader via NOMADS filter.

Variables are fixed by the authoritative contract in gfs_repo/var_contract.py.

Usage:
  python gfs_fetch.py --date 2026-02-26 --cycle 00 \
      --bbox "112.25,31.5,122.0,34.5" \
      --fh 0:23 --out ./gfs_data
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from gfs_repo.var_contract import DEFAULT_VARS, VAR_MAPPING, to_nomads_lev

BASE_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_params(fname: str, cycle: str, date_str: str,
                 vars_: list[str], bbox: dict) -> dict:
    params: dict = {
        "file":       fname,
        "subregion":  "on",
        "leftlon":    bbox["west"],
        "rightlon":   bbox["east"],
        "toplat":     bbox["north"],
        "bottomlat":  bbox["south"],
        "dir":        f"/gfs.{date_str}/{cycle}/atmos",
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


def download_one(url: str, params: dict, dest: Path,
                 retries: int, timeout: int) -> int:
    """Return file size on success; raise RuntimeError after all retries."""
    headers = {"User-Agent": "gfs_fetch/1.0"}
    fname = params["file"]

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, stream=True,
                                headers=headers, timeout=timeout)
            resp.raise_for_status()

            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    fh.write(chunk)

            with open(dest, "rb") as fh:
                magic = fh.read(4)

            if magic == b"GRIB":
                return dest.stat().st_size

            print(f"    [!] {fname}: not GRIB (attempt {attempt}/{retries})",
                  file=sys.stderr)

        except Exception as exc:
            print(f"    [!] {fname}: {exc} (attempt {attempt}/{retries})",
                  file=sys.stderr)

        dest.unlink(missing_ok=True)
        if attempt < retries:
            time.sleep(2 ** (attempt - 1))  # 0 s, 1 s, 2 s …

    raise RuntimeError(f"Failed to download {fname} after {retries} attempts.")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def fetch(date: str, cycle: str, bbox: dict, out: Path,
          fh_start: int, fh_end: int,
          retries: int, timeout: int) -> None:

    date_str  = date.replace("-", "")
    init_time = f"{date_str}T{cycle}00Z"
    dest_dir  = out / init_time
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"init_time : {init_time}")
    print(f"dest      : {dest_dir}")
    print(f"fh        : {fh_start}–{fh_end}")
    print(f"vars      : {', '.join(DEFAULT_VARS)}")
    print()

    files_meta: list[dict] = []

    for fh in range(fh_start, fh_end + 1):
        fname  = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
        dest   = dest_dir / fname
        params = build_params(fname, cycle, date_str, DEFAULT_VARS, bbox)

        print(f"  f{fh:03d}  {fname} … ", end="", flush=True)
        size = download_one(BASE_URL, params, dest, retries, timeout)
        print(f"{size:,} B")

        files_meta.append({"name": fname, "fh": fh, "size": size})

    manifest = {
        "init_time":    init_time,
        "date_utc":     date_str,
        "cycle":        cycle,
        "bbox":         bbox,
        "fh_range":     {"start": fh_start, "end": fh_end},
        "vars":         DEFAULT_VARS,
        "files":        files_meta,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = dest_dir / "manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)

    print(f"\nmanifest  : {manifest_path}")
    print(f"done      : {len(files_meta)} files")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="Stateless GFS 0.25° downloader via NOMADS filter.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Downloads all {len(DEFAULT_VARS)} contract variables: "
               f"{', '.join(DEFAULT_VARS)}",
    )
    p.add_argument("--date",    required=True,
                   help="UTC date, YYYY-MM-DD")
    p.add_argument("--cycle",   required=True, choices=["00", "06", "12", "18"],
                   help="Model cycle")
    p.add_argument("--bbox",    required=True,
                   help="west,south,east,north (degrees)")
    p.add_argument("--out",     default="./gfs_data",
                   help="Output root (default: ./gfs_data)")
    p.add_argument("--fh",      default="0:23",
                   help="Forecast-hour range start:end (default: 0:23)")
    p.add_argument("--retries", type=int, default=3,
                   help="Retries per file (default: 3)")
    p.add_argument("--timeout", type=int, default=30,
                   help="HTTP timeout in seconds (default: 30)")
    args = p.parse_args()

    # --date
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"error: --date must be YYYY-MM-DD, got '{args.date}'")

    # --bbox  west,south,east,north
    try:
        w, s, e, n = [float(x) for x in args.bbox.split(",")]
    except ValueError:
        sys.exit("error: --bbox must be 'west,south,east,north'")
    bbox = {"west": w, "south": s, "east": e, "north": n}

    # --fh
    try:
        fh_start, fh_end = [int(x) for x in args.fh.split(":")]
    except ValueError:
        sys.exit("error: --fh must be 'start:end', e.g. '0:23'")

    fetch(
        date=args.date,
        cycle=args.cycle,
        bbox=bbox,
        out=Path(args.out),
        fh_start=fh_start,
        fh_end=fh_end,
        retries=args.retries,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    main()
