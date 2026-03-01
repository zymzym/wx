"""
Authoritative GFS variable contract.

Both gfs_fetch.py and hist_fetch.py import from here as their sole source of
variable definitions.  The --vars CLI flag does not exist in either script;
all variable requests must come from DEFAULT_VARS / VAR_MAPPING below.

VAR_MAPPING entry layout:
    canonical_key → {"var": GRIB2_abbrev, "lev": human_readable_level}

    "var"  — matches NOMADS filter var_* params AND the S3 .idx variable field
    "lev"  — matches the S3 .idx level field (spaces, human-readable)
             NOMADS lev_* param names are derived via to_nomads_lev()
"""

VAR_MAPPING: dict[str, dict[str, str]] = {
    # ── 2-m diagnostics ──────────────────────────────────────────────────────
    "t2m":    {"var": "TMP",   "lev": "2 m above ground"},
    "d2m":    {"var": "DPT",   "lev": "2 m above ground"},
    "rh2m":   {"var": "RH",    "lev": "2 m above ground"},

    # ── 10-m / 100-m wind ────────────────────────────────────────────────────
    "u10":    {"var": "UGRD",  "lev": "10 m above ground"},
    "v10":    {"var": "VGRD",  "lev": "10 m above ground"},
    "u100m":  {"var": "UGRD",  "lev": "100 m above ground"},
    "v100m":  {"var": "VGRD",  "lev": "100 m above ground"},

    # ── pressure-level wind ──────────────────────────────────────────────────
    "u925":   {"var": "UGRD",  "lev": "925 mb"},
    "v925":   {"var": "VGRD",  "lev": "925 mb"},
    "u850":   {"var": "UGRD",  "lev": "850 mb"},
    "v850":   {"var": "VGRD",  "lev": "850 mb"},
    "u700":   {"var": "UGRD",  "lev": "700 mb"},
    "v700":   {"var": "VGRD",  "lev": "700 mb"},
    "u500":   {"var": "UGRD",  "lev": "500 mb"},
    "v500":   {"var": "VGRD",  "lev": "500 mb"},

    # ── surface / single-level ───────────────────────────────────────────────
    "gust10m": {"var": "GUST", "lev": "surface"},
    "psfc":   {"var": "PRES",  "lev": "surface"},
    "mslp":   {"var": "PRMSL", "lev": "mean sea level"},
    "hpbl":   {"var": "HPBL",  "lev": "surface"},

    # ── cloud / precip / moisture ─────────────────────────────────────────────
    "tcc":    {"var": "TCDC",  "lev": "entire atmosphere"},
    "tp":     {"var": "APCP",  "lev": "surface"},
    "prate":  {"var": "PRATE", "lev": "surface"},
    "pwat":   {"var": "PWAT",  "lev": "entire atmosphere (considered as a single layer)"},
    "cape":   {"var": "CAPE",  "lev": "surface"},

    # ── radiation ────────────────────────────────────────────────────────────
    "dswrf":  {"var": "DSWRF", "lev": "surface"},
    "dlwrf":  {"var": "DLWRF", "lev": "surface"},

    # ── land / snow ───────────────────────────────────────────────────────────
    "land":   {"var": "LAND",  "lev": "surface"},
    "snod":   {"var": "SNOD",  "lev": "surface"},
}

DEFAULT_VARS: list[str] = list(VAR_MAPPING.keys())

# ── NOMADS level-name conversion ──────────────────────────────────────────────
# NOMADS filter uses underscored level names.
# The "lev" fields above use the exact S3 .idx level labels, so a plain
# space→underscore replacement produces the correct NOMADS lev_* param:
#   "2 m above ground"                       → lev_2_m_above_ground
#   "925 mb"                                 → lev_925_mb
#   "entire atmosphere"                      → lev_entire_atmosphere           (TCDC)
#   "entire atmosphere (considered ...)"     → lev_entire_atmosphere_(...)     (PWAT)
#   "mean sea level"                         → lev_mean_sea_level

def to_nomads_lev(lev: str) -> str:
    """Return the NOMADS filter lev_* param name for a human-readable level."""
    return lev.replace(" ", "_")
