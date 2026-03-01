#!/usr/bin/env python3
"""
check_contract.py — 验证 var_contract 中每个变量能否在两个接口中被找到。

Interface A (NOMADS filter):
  检查派生的 lev_* / var_* 参数名格式是否符合预期

Interface B (S3 .idx):
  下载一个真实的 .idx 文件，逐条匹配 VAR_MAPPING

Usage:
  python check_contract.py [--date YYYY-MM-DD] [--cycle 00|06|12|18] [--fh N]
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests
from gfs_repo.var_contract import DEFAULT_VARS, VAR_MAPPING, to_nomads_lev

S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
_HDRS   = {"User-Agent": "gfs_fetch/1.0"}

# ── ANSI colours ─────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
RESET  = "\033[0m"


def ok(s):  return f"{GREEN}✓{RESET} {s}"
def err(s): return f"{RED}✗{RESET} {s}"
def warn(s): return f"{YELLOW}?{RESET} {s}"


# ── Interface A: NOMADS params audit ─────────────────────────────────────────

def audit_nomads() -> None:
    print("=" * 60)
    print("Interface A — NOMADS filter params (derived from contract)")
    print("=" * 60)

    var_params:  dict[str, set[str]] = {}   # var_* → set of shortnames
    lev_params:  dict[str, set[str]] = {}   # lev_* → set of shortnames

    for key in DEFAULT_VARS:
        entry   = VAR_MAPPING[key]
        vp      = f"var_{entry['var']}"
        lp      = f"lev_{to_nomads_lev(entry['lev'])}"
        var_params.setdefault(vp, set()).add(key)
        lev_params.setdefault(lp, set()).add(key)

    print(f"\n  {len(var_params)} unique var_* params:")
    for p, keys in sorted(var_params.items()):
        print(f"    {p:45s}  ← {', '.join(sorted(keys))}")

    print(f"\n  {len(lev_params)} unique lev_* params:")
    for p, keys in sorted(lev_params.items()):
        print(f"    {p:60s}  ← {', '.join(sorted(keys))}")


# ── Interface B: S3 idx audit ─────────────────────────────────────────────────

def fetch_idx(date_str: str, cycle: str, fh: int, timeout: int) -> list[str]:
    fname = f"gfs.t{cycle}z.pgrb2.0p25.f{fh:03d}"
    url   = f"{S3_BASE}/gfs.{date_str}/{cycle}/atmos/{fname}.idx"
    print(f"\n  Fetching: {url}")
    resp  = requests.get(url, headers=_HDRS, timeout=timeout)
    resp.raise_for_status()
    return resp.text.strip().splitlines()


def audit_s3(date_str: str, cycle: str, fh: int, timeout: int) -> tuple[list[str], list[str]]:
    print("\n" + "=" * 60)
    print(f"Interface B — S3 .idx  ({date_str} cycle={cycle} f{fh:03d})")
    print("=" * 60)

    lines = fetch_idx(date_str, cycle, fh, timeout)
    print(f"  idx entries: {len(lines)}\n")

    found:   list[str] = []
    missing: list[str] = []

    for key in DEFAULT_VARS:
        entry   = VAR_MAPPING[key]
        pattern = f":{entry['var']}:{entry['lev']}"
        hits    = [l for l in lines if pattern in l]
        if hits:
            lev_field = hits[0].split(":")[4] if len(hits[0].split(":")) > 4 else "?"
            v, l = entry["var"], entry["lev"]
            print(f"  {ok(f'{key:12s}  {v:6s} / {l:35s}  → {lev_field}')}")
            found.append(key)
        else:
            v, l = entry["var"], entry["lev"]
            print(f"  {err(f'{key:12s}  {v:6s} / {l:35s}  NOT FOUND in idx')}")
            missing.append(key)

    return found, missing


# ── Cross-interface consistency summary ───────────────────────────────────────

def summary(found: list[str], missing: list[str]) -> None:
    total = len(DEFAULT_VARS)
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    print(f"  Contract vars  : {total}")
    print(f"  Found in S3    : {GREEN}{len(found)}{RESET}")
    if missing:
        print(f"  Missing in S3  : {RED}{len(missing)}{RESET}  →  {', '.join(missing)}")
    else:
        print(f"  Missing in S3  : {GREEN}0 — all variables verified{RESET}")

    if missing:
        print(f"\n{YELLOW}Note:{RESET} missing vars may exist under a different level label.")
        print("  Check the raw idx lines above and update var_contract.py if needed.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # Default: most recent completed cycle (yesterday 00Z is safe for S3)
    default_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    p = argparse.ArgumentParser(description="Verify var_contract against NOMADS and S3.")
    p.add_argument("--date",    default=default_date,
                   help=f"UTC date YYYY-MM-DD (default: {default_date})")
    p.add_argument("--cycle",   default="00", choices=["00", "06", "12", "18"])
    p.add_argument("--fh",      type=int, default=3,
                   help="Forecast hour to check (default: 3; use ≥1 for accumulated fields)")
    p.add_argument("--timeout", type=int, default=30)
    args = p.parse_args()

    date_str = args.date.replace("-", "")

    audit_nomads()

    try:
        found, missing = audit_s3(date_str, args.cycle, args.fh, args.timeout)
    except Exception as exc:
        print(f"\n{RED}ERROR fetching S3 idx: {exc}{RESET}", file=sys.stderr)
        sys.exit(1)

    summary(found, missing)
    sys.exit(1 if missing else 0)


if __name__ == "__main__":
    main()
