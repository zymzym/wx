#!/usr/bin/env python3
"""
gfs_to_15min.py — GFS GRIB2 → 15-minute time series for ALL lat/lon grid points.

Variable types are inferred ENTIRELY from GRIB metadata (stepType, stepRange).
No variable names are hard-coded.

Processing pipeline (per target date):
  1. Scan dirs → find init-cycles that overlap the target date
  2. Read records only from those relevant dirs
  3. Compute min_set intersection from the relevant dirs
  4. Compute hourly values (diff acc_segment, weighted-diff ave_segment, direct instant)
  5. Dedup by (var_key, valid_time) → keep latest init_time
  6. Expand to 96 × 15-min slots × all grid points
  7. Save CSV to data_js_csv/

Usage:
  python gfs_to_15min.py \\
      --data-dir ./data_js \\
      [--date 2025-09-01 | --all] \\
      [--out-dir ./data_js_csv]
"""

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import eccodes
import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Variable type constants
# ---------------------------------------------------------------------------
INSTANT     = "instant"       # stepType == 'instant'
ACC_SEGMENT = "acc_segment"   # stepType == 'accum',  expanding window with periodic reset
AVE_SEGMENT = "ave_segment"   # stepType == 'avg',    expanding window with periodic reset

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class RawRecord:
    """One GRIB2 message worth of data for all grid points."""
    var_key:     tuple
    step_type:   str
    step_range:  str
    seg_start:   int
    seg_end:     int
    init_time:   datetime
    valid_time:  datetime
    lead_hours:  int
    values:      np.ndarray   # shape: (npoints,)
    lats:        np.ndarray   # shape: (npoints,)
    lons:        np.ndarray   # shape: (npoints,)
    param_extra: str

@dataclass
class HourlyRecord:
    """One hourly processed value for all grid points."""
    var_key:    tuple
    var_type:   str
    init_time:  datetime
    valid_time: datetime
    values:     np.ndarray   # shape: (npoints,)
    lats:       np.ndarray   # shape: (npoints,)
    lons:       np.ndarray   # shape: (npoints,)

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def parse_dir_init_time(dirname: str) -> Optional[datetime]:
    """'20250901T0000Z' → datetime(2025,9,1,0,0, tzinfo=utc)."""
    m = re.match(r'^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})Z$', dirname)
    if not m:
        return None
    yr, mo, dy, hh, mm = (int(x) for x in m.groups())
    return datetime(yr, mo, dy, hh, mm, tzinfo=timezone.utc)


def parse_step_range(step_range: str) -> tuple[int, int]:
    """'6-12' → (6, 12); '1' → (0, 1)."""
    if '-' in step_range:
        parts = step_range.split('-')
        return int(parts[0]), int(parts[1])
    else:
        return 0, int(step_range)


def eccodes_dt(validity_date: int, validity_time: int) -> datetime:
    """Convert eccodes validityDate/validityTime ints to UTC datetime."""
    s = str(validity_date)
    yr, mo, dy = int(s[:4]), int(s[4:6]), int(s[6:8])
    hh = validity_time // 100
    mm = validity_time % 100
    return datetime(yr, mo, dy, hh, mm, tzinfo=timezone.utc)


def var_key_to_colname(var_key: tuple, param_extra: str = "") -> str:
    """(shortName, typeOfLevel, level) → human-readable column name."""
    name, tol, lev = var_key
    if name == "unknown":
        name = f"unk{param_extra}"
    level_suffix = {
        "surface":                    "sfc",
        "meanSea":                    "msl",
        "atmosphere":                 "atm",
        "atmosphereSingleLayer":      "atm1l",
        "heightAboveGround":          f"{lev}m",
        "isobaricInhPa":              f"{lev}hPa",
    }.get(tol, f"{tol}_{lev}")
    return f"{name}_{level_suffix}"


def compute_grid_latlons(lat0: float, lon0: float,
                          dlat: float, dlon: float,
                          ni: int, nj: int,
                          jpos: bool) -> tuple[np.ndarray, np.ndarray]:
    """Compute flat lat/lon arrays for all grid points (row-major j×i order).

    Returns lats, lons each of shape (nj*ni,).
    """
    j_arr = np.arange(nj)
    i_arr = np.arange(ni)
    lats_1d = lat0 + j_arr * dlat if jpos else lat0 - j_arr * dlat
    lons_1d = (lon0 + i_arr * dlon) % 360
    lats = np.repeat(lats_1d, ni)   # shape: (nj*ni,)
    lons = np.tile(lons_1d, nj)     # shape: (nj*ni,)
    return lats, lons


# ---------------------------------------------------------------------------
# Step 1: Read GRIB2 files and extract full grid values
# ---------------------------------------------------------------------------

def read_file(file_path: Path, init_time: datetime) -> list[RawRecord]:
    """Read all messages from one GRIB2 file, extract full grid values."""
    records: list[RawRecord] = []
    grid_ll_cache: dict = {}   # (ni,nj,lat0,lon0,dlat,dlon,jpos) → (lats, lons)

    with open(file_path, 'rb') as fh:
        while True:
            try:
                msg = eccodes.codes_grib_new_from_file(fh)
            except eccodes.CodesInternalError:
                break
            if msg is None:
                break
            try:
                short_name  = eccodes.codes_get(msg, 'shortName')
                tol         = eccodes.codes_get(msg, 'typeOfLevel')
                level       = eccodes.codes_get(msg, 'level')
                step_type   = eccodes.codes_get(msg, 'stepType')
                step_range  = eccodes.codes_get(msg, 'stepRange')
                val_date    = eccodes.codes_get(msg, 'validityDate')
                val_time    = eccodes.codes_get(msg, 'validityTime')

                if short_name == 'unknown':
                    try:
                        disc = eccodes.codes_get(msg, 'discipline')
                        cat  = eccodes.codes_get(msg, 'parameterCategory')
                        num  = eccodes.codes_get(msg, 'parameterNumber')
                        param_extra = f"_d{disc}c{cat}p{num}"
                    except Exception:
                        param_extra = "_unknown"
                else:
                    param_extra = ""

                var_key    = (short_name, tol, level)
                seg_start, seg_end = parse_step_range(step_range)
                valid_time = eccodes_dt(val_date, val_time)
                lead_hours = int((valid_time - init_time).total_seconds() // 3600)

                lat0 = eccodes.codes_get(msg, 'latitudeOfFirstGridPoint') / 1e6
                lon0 = eccodes.codes_get(msg, 'longitudeOfFirstGridPoint') / 1e6
                dlat = eccodes.codes_get(msg, 'jDirectionIncrementInDegrees')
                dlon = eccodes.codes_get(msg, 'iDirectionIncrementInDegrees')
                ni   = eccodes.codes_get(msg, 'Ni')
                nj   = eccodes.codes_get(msg, 'Nj')
                jpos = bool(eccodes.codes_get(msg, 'jScansPositively'))

                cache_key = (ni, nj, lat0, lon0, dlat, dlon, jpos)
                if cache_key not in grid_ll_cache:
                    grid_ll_cache[cache_key] = compute_grid_latlons(
                        lat0, lon0, dlat, dlon, ni, nj, jpos
                    )
                lats, lons = grid_ll_cache[cache_key]

                values = eccodes.codes_get_array(msg, 'values').astype(np.float64)

            except Exception:
                eccodes.codes_release(msg)
                continue

            eccodes.codes_release(msg)
            records.append(RawRecord(
                var_key     = var_key,
                step_type   = step_type,
                step_range  = step_range,
                seg_start   = seg_start,
                seg_end     = seg_end,
                init_time   = init_time,
                valid_time  = valid_time,
                lead_hours  = lead_hours,
                values      = values,
                lats        = lats,
                lons        = lons,
                param_extra = param_extra,
            ))
    return records


def read_dir_records(dir_path: Path, init_time: datetime) -> list[RawRecord]:
    """Read all GRIB2 files in a directory."""
    records: list[RawRecord] = []
    for fpath in sorted(dir_path.glob("gfs.t??z.pgrb2.0p25.f[0-9][0-9][0-9]")):
        records.extend(read_file(fpath, init_time))
    return records


# ---------------------------------------------------------------------------
# Step 1 report: classify var_key types
# ---------------------------------------------------------------------------

def classify_var_types(all_records: list[RawRecord]) -> dict:
    """Determine the temporal type for each (var_key, param_extra) pair."""
    seen: dict = {}
    for r in all_records:
        key = (r.var_key, r.param_extra)
        if key in seen:
            seen[key]['step_types'].add(r.step_type)
            seen[key]['step_ranges'].add(r.step_range)
        else:
            seen[key] = {
                'step_types':  {r.step_type},
                'step_ranges': {r.step_range},
            }

    result = {}
    for (var_key, param_extra), info in seen.items():
        st = info['step_types']
        ranges = info['step_ranges']

        if 'instant' in st and len(st) == 1:
            vtype = INSTANT
        elif 'accum' in st:
            vtype = ACC_SEGMENT
        elif 'avg' in st:
            vtype = AVE_SEGMENT
        else:
            vtype = INSTANT

        descriptor_raw = f"stepType={','.join(sorted(st))}  stepRange samples={sorted(ranges)[:6]}"
        if len(ranges) == 1:
            interval_hint = list(ranges)[0]
        else:
            sorted_ranges = sorted(ranges, key=lambda x: parse_step_range(x)[1])
            interval_hint = f"{sorted_ranges[0]} … {sorted_ranges[-1]}"

        result[(var_key, param_extra)] = {
            'type':           vtype,
            'descriptor_raw': descriptor_raw,
            'interval_hint':  interval_hint,
        }
    return result


# ---------------------------------------------------------------------------
# Step 2: Compute hourly values from raw records (vectorized over grid)
# ---------------------------------------------------------------------------

def compute_hourly_values(raw_records: list[RawRecord],
                          var_type_map: dict) -> list[HourlyRecord]:
    """Convert raw GRIB records → one HourlyRecord per (var_key, init_time, valid_time).

    All arithmetic is vectorized over the full grid (values are numpy arrays).
    """
    hourly: list[HourlyRecord] = []

    groups: dict = defaultdict(list)
    for r in raw_records:
        groups[(r.init_time, r.var_key, r.param_extra, r.step_type, r.seg_start)].append(r)

    for (init_time, var_key, param_extra, step_type, seg_start), recs in groups.items():
        if step_type == 'instant':
            vtype = INSTANT
        elif step_type == 'accum':
            vtype = ACC_SEGMENT
        elif step_type == 'avg':
            vtype = AVE_SEGMENT
        else:
            vtype = INSTANT

        lats = recs[0].lats
        lons = recs[0].lons

        if vtype == INSTANT:
            for r in recs:
                hourly.append(HourlyRecord(
                    var_key    = var_key,
                    var_type   = vtype,
                    init_time  = init_time,
                    valid_time = r.valid_time,
                    values     = r.values,
                    lats       = lats,
                    lons       = lons,
                ))

        elif vtype == ACC_SEGMENT:
            recs_sorted = sorted(recs, key=lambda x: x.seg_end)
            prev_acc = np.zeros_like(recs_sorted[0].values)
            for rec in recs_sorted:
                inc = rec.values - prev_acc
                hourly.append(HourlyRecord(
                    var_key    = var_key,
                    var_type   = vtype,
                    init_time  = init_time,
                    valid_time = rec.valid_time,
                    values     = inc,
                    lats       = lats,
                    lons       = lons,
                ))
                prev_acc = rec.values.copy()

        elif vtype == AVE_SEGMENT:
            recs_sorted = sorted(recs, key=lambda x: x.seg_end)
            prev_val   = np.zeros_like(recs_sorted[0].values)
            prev_width = 0
            for rec in recs_sorted:
                width      = rec.seg_end - seg_start
                hourly_avg = rec.values * width - prev_val * prev_width
                hourly.append(HourlyRecord(
                    var_key    = var_key,
                    var_type   = vtype,
                    init_time  = init_time,
                    valid_time = rec.valid_time,
                    values     = hourly_avg,
                    lats       = lats,
                    lons       = lons,
                ))
                prev_val   = rec.values.copy()
                prev_width = width

    return hourly


# ---------------------------------------------------------------------------
# Step 4: Dedup by (var_key, valid_time) → keep latest init_time
# ---------------------------------------------------------------------------

def dedup_hourly(hourly_records: list[HourlyRecord]) -> list[HourlyRecord]:
    """For each (var_key, valid_time), keep the record with the latest init_time."""
    best: dict = {}
    for r in hourly_records:
        key = (r.var_key, r.valid_time)
        if key not in best or r.init_time > best[key].init_time:
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# Step 3+5: Expand hourly → 96 × 15-min slots × all grid points
# ---------------------------------------------------------------------------

def build_15min_grid(target_date: date) -> pd.DatetimeIndex:
    """Return DatetimeIndex of 96 UTC times for the given date (00:00 to 23:45)."""
    start = datetime(target_date.year, target_date.month, target_date.day,
                     0, 0, tzinfo=timezone.utc)
    return pd.date_range(start=start, periods=96, freq='15min')


def expand_to_15min(hourly_records: list[HourlyRecord],
                    var_type_map: dict,
                    param_extra_map: dict,
                    target_date: date,
                    min_set: set,
                    out_path: Path) -> int:
    """Stream 15-min CSV to out_path, processing one hour at a time.

    Peak memory: one (npoints × ncols) slice instead of (96 × npoints × ncols).
    Returns total rows written.

    Rules:
      instant / ave_segment → step-hold: same value at [H:00, H:15, H:30, H:45]
      acc_segment           → split:     hourly_inc / 4 per 15-min slot
    """
    if not hourly_records:
        return 0

    grid_ts   = build_15min_grid(target_date)
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(hours=23)

    # Build column index from min_set
    colname_map: dict = {}
    for (var_key, param_extra) in min_set:
        col = var_key_to_colname(var_key, param_extra)
        colname_map[col] = (var_key, param_extra)
    cols    = sorted(colname_map.keys())
    col_idx = {c: i for i, c in enumerate(cols)}

    # Determine grid from first record
    ref     = hourly_records[0]
    lats    = ref.lats    # shape: (npoints,)
    lons    = ref.lons
    npoints = len(lats)

    # Pre-group records by valid_time (only min_set vars within the day)
    by_hour: dict = defaultdict(list)
    for r in hourly_records:
        param_extra = param_extra_map.get(r.var_key, "")
        if (r.var_key, param_extra) not in min_set:
            continue
        if day_start <= r.valid_time <= day_end:
            by_hour[r.valid_time].append(r)

    date_str      = target_date.isoformat()
    total_rows    = 0
    first_write   = True
    out_path.unlink(missing_ok=True)

    for slot_h in range(24):
        valid_time = day_start + timedelta(hours=slot_h)
        slot_base  = slot_h * 4

        # One (npoints × ncols) slice — peak ~220 MB for global 0.25° grid
        hour_arr = np.full((npoints, len(cols)), np.nan, dtype=np.float64)

        for r in by_hour.get(valid_time, []):
            param_extra = param_extra_map.get(r.var_key, "")
            col = var_key_to_colname(r.var_key, param_extra)
            ci  = col_idx.get(col)
            if ci is None:
                continue
            if r.var_type == ACC_SEGMENT:
                hour_arr[:, ci] = r.values / 4.0
            else:
                hour_arr[:, ci] = r.values

        # Expand to 4 × 15-min slots and write
        df_hour = pd.DataFrame(np.tile(hour_arr, (4, 1)), columns=cols)
        df_hour.insert(0, 'date',           date_str)
        df_hour.insert(1, 'time_index_15m', np.repeat(
                            np.arange(slot_base, slot_base + 4), npoints))
        df_hour.insert(2, 'valid_time',     np.repeat(
                            grid_ts[slot_base:slot_base + 4], npoints))
        df_hour.insert(3, 'lat',            np.tile(lats, 4))
        df_hour.insert(4, 'lon',            np.tile(lons, 4))

        df_hour.to_csv(out_path, mode='a', header=first_write, index=False)
        first_write = False
        total_rows += len(df_hour)

    return total_rows


# ---------------------------------------------------------------------------
# Min-set computation across all directories
# ---------------------------------------------------------------------------

def compute_min_set(dir_records_map: dict) -> set:
    """Compute intersection of (var_key, param_extra) sets across all dirs."""
    sets = []
    for dirname, records in dir_records_map.items():
        var_set = {(r.var_key, r.param_extra) for r in records}
        sets.append(var_set)
    if not sets:
        return set()
    min_set = sets[0]
    for s in sets[1:]:
        min_set = min_set & s
    return min_set


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def scan_data_dirs(data_dir: Path) -> list[tuple[datetime, Path]]:
    """Return sorted [(init_time, dir_path)] for all valid data directories."""
    result = []
    for p in sorted(data_dir.iterdir()):
        if not p.is_dir():
            continue
        it = parse_dir_init_time(p.name)
        if it is not None:
            result.append((it, p))
    result.sort(key=lambda x: x[0])
    return result


def dirs_for_date(all_dirs: list[tuple[datetime, Path]],
                  target_date: date) -> list[tuple[datetime, Path]]:
    """Return dirs whose forecast range overlaps [target_date 00:00, target_date 23:00] UTC."""
    day_start = datetime(target_date.year, target_date.month, target_date.day,
                         0, 0, tzinfo=timezone.utc)
    day_end   = day_start + timedelta(hours=23)
    relevant = []
    for init_time, dpath in all_dirs:
        cov_start = init_time
        cov_end   = init_time + timedelta(hours=23)
        if cov_start <= day_end and cov_end >= day_start:
            relevant.append((init_time, dpath))
    return relevant


def print_classification_report(var_type_map: dict,
                                  param_extra_map: dict,
                                  min_set: set) -> None:
    """Print the Step 1 classification table to stdout."""
    print("\n" + "=" * 80)
    print("STEP 1: Variable classification table")
    print("=" * 80)
    fmt = "{:<35} {:<14} {:<12} {}"
    print(fmt.format("var_key (name, typeOfLevel, level)", "type",
                     "interval_hint", "descriptor_raw"))
    print("-" * 80)
    for (var_key, param_extra), info in sorted(var_type_map.items(),
                                               key=lambda x: str(x[0])):
        col = var_key_to_colname(var_key, param_extra)
        in_min = " *" if (var_key, param_extra) in min_set else ""
        print(fmt.format(
            f"{col}{in_min}",
            info['type'],
            info['interval_hint'],
            info['descriptor_raw'][:70],
        ))
    print("\n* = in min_set (present in ALL directories)\n")


def print_mapping_rules() -> None:
    """Print the 15-minute mapping rules confirmation."""
    print("=" * 80)
    print("STEP 3: 15-minute mapping rules")
    print("=" * 80)
    print("  instant         → step-hold: same value at [H:00, H:15, H:30, H:45]")
    print("  ave_segment     → step-hold: same 1-hour mean at [H:00, H:15, H:30, H:45]")
    print("  acc_segment     → split:     hourly_inc / 4 per 15-min slot")
    print()
    print("STEP 4: Dedup rule")
    print("=" * 80)
    print("  For same (var_key, valid_time): keep record with latest init_time")
    print()


def run(data_dir: Path, target_dates: list[date], out_dir: Path, verbose: bool) -> None:

    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Scan directories ──────────────────────────────────────────────────
    all_dirs = scan_data_dirs(data_dir)
    if not all_dirs:
        sys.exit(f"No valid data directories found in {data_dir}")

    print(f"\nFound {len(all_dirs)} init-cycle directories.")

    if verbose:
        print_mapping_rules()

    # ── 2. Process each target date independently ────────────────────────────
    for tdate in sorted(target_dates):
        print(f"\n{'─' * 60}")
        print(f"Processing date: {tdate}")

        relevant_dirs = dirs_for_date(all_dirs, tdate)
        if not relevant_dirs:
            print(f"  [warn] No directories cover {tdate}, skipping.")
            continue

        print(f"  Using {len(relevant_dirs)} init-cycle(s):")
        for it, dp in relevant_dirs:
            print(f"    {dp.name}")

        # Read records only from dirs relevant to this date
        dir_records_map: dict = {}
        for init_time, dir_path in relevant_dirs:
            recs = read_dir_records(dir_path, init_time)
            dir_records_map[dir_path.name] = recs
            npts = len(recs[0].values) if recs else 0
            print(f"    {dir_path.name}: {len(recs)} records  grid_points={npts}")

        # Compute min_set and var_type_map from this date's dirs only
        min_set = compute_min_set(dir_records_map)
        all_day_records = [r for recs in dir_records_map.values() for r in recs]
        var_type_map = classify_var_types(all_day_records)

        param_extra_map: dict = {}
        for (var_key, param_extra) in var_type_map:
            param_extra_map[var_key] = param_extra

        print(f"  min_set: {len(min_set)} var_keys")

        if verbose:
            print_classification_report(var_type_map, param_extra_map, min_set)

        # Filter records to valid_time within this day
        day_start = datetime(tdate.year, tdate.month, tdate.day,
                             0, 0, tzinfo=timezone.utc)
        day_end   = day_start + timedelta(hours=23)
        raw = [r for r in all_day_records if day_start <= r.valid_time <= day_end]

        if not raw:
            print(f"  [warn] No records found for {tdate}, skipping.")
            continue

        hourly       = compute_hourly_values(raw, var_type_map)
        hourly_dedup = dedup_hourly(hourly)

        out_path = out_dir / f"gfs_15min_{tdate.isoformat()}.csv"
        nrows = expand_to_15min(hourly_dedup, var_type_map,
                                param_extra_map, tdate, min_set, out_path)
        print(f"  Saved → {out_path}  ({nrows} rows × {len(min_set) + 5} cols)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(
        description="GFS GRIB2 → 15-minute time series for all lat/lon grid points.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--data-dir", default="./data_js",
                   help="Root directory containing init-cycle subdirs (default: ./data_js)")
    p.add_argument("--date",  help="Single UTC date to process (YYYY-MM-DD)")
    p.add_argument("--all",   action="store_true",
                   help="Process all dates covered by available data")
    p.add_argument("--out-dir", default="./data_js_csv",
                   help="Output directory (default: ./data_js_csv)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print sample rows after each date")
    args = p.parse_args()

    if not args.date and not args.all:
        p.error("Specify either --date YYYY-MM-DD or --all")

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        sys.exit(f"error: --data-dir '{data_dir}' not found")

    out_dir = Path(args.out_dir)

    all_dirs = scan_data_dirs(data_dir)
    if args.all:
        target_dates: set[date] = set()
        for init_time, _ in all_dirs:
            for h in range(24):
                vt = init_time + timedelta(hours=h)
                target_dates.add(vt.date())
        dates_list = sorted(target_dates)
    else:
        try:
            d = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            sys.exit(f"error: --date must be YYYY-MM-DD, got '{args.date}'")
        dates_list = [d]

    print(f"Target dates: {[str(d) for d in dates_list]}")

    run(
        data_dir     = data_dir,
        target_dates = dates_list,
        out_dir      = out_dir,
        verbose      = args.verbose,
    )


if __name__ == "__main__":
    main()
