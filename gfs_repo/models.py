from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict
from pathlib import Path

# --- Constants & Enums (R12) ---

class PartitionStatus(str, Enum):
    MISSING = "missing"
    INCOMPLETE = "incomplete"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"

class ErrorCode(str, Enum):
    E_INVALID_SIGNATURE = "E_INVALID_SIGNATURE"
    E_INVALID_INIT_TIME = "E_INVALID_INIT_TIME"
    E_LOCKED = "E_LOCKED"
    E_DOWNLOAD_HTTP = "E_DOWNLOAD_HTTP"
    E_DOWNLOAD_HTML = "E_DOWNLOAD_HTML"
    E_VERIFY_FAILED = "E_VERIFY_FAILED"
    E_RAW_MISSING = "E_RAW_MISSING"

# --- Data Models (R01, R02) ---

@dataclass
class BBox:
    north: float
    west: float
    south: float
    east: float

@dataclass
class Signature:
    model: str      # e.g., "gfs"
    grid: str       # e.g., "0p25"
    step: str       # e.g., "1hr"
    source: str     # e.g., "nomads_filter"
    bbox: BBox
    varset: str     # e.g., "v1"
    var_keys: List[str]

    def get_rel_path(self) -> Path:
        """生成基于签名的相对路径: model/grid/step/source"""
        return Path(self.model) / self.grid / self.step / self.source

@dataclass
class PartitionSpec:
    signature: Signature
    init_time: str                  # Format: YYYYMMDDTHH00Z
    forecast_hours: Dict[str, int]  # {"start": 0, "end": 120}

    def __post_init__(self):
        # R01: Basic validation for init_time format
        try:
            # Note: Z is a literal character here, implying UTC
            datetime.strptime(self.init_time, "%Y%m%dT%H%MZ")
        except ValueError:
            raise ValueError(f"{ErrorCode.E_INVALID_INIT_TIME.value}: {self.init_time}")
        
        # Validate forecast_hours
        if "start" not in self.forecast_hours or "end" not in self.forecast_hours:
             raise ValueError(f"{ErrorCode.E_INVALID_SIGNATURE.value}: forecast_hours missing start/end")

    @property
    def init_dt(self) -> datetime:
        """Helper to get datetime object from init_time string (UTC)"""
        # Note: Z is a literal character here
        return datetime.strptime(self.init_time, "%Y%m%dT%H%MZ").replace(tzinfo=timezone.utc)

@dataclass
class VerifyReport:
    ok: bool
    status: PartitionStatus
    problems: List[str] = field(default_factory=list)

@dataclass
class PutResult:
    result: str # created|updated|skipped
    already_complete: bool
    raw_path: str
    manifest_path: str

@dataclass
class QuarantineResult:
    moved: bool
    src: str
    dst: str

@dataclass
class ResolveResult:
    chosen_init_time: str
    reason: str
    candidates_tried: List[str]

@dataclass
class EnsureResult:
    status: PartitionStatus
    raw_result: str
    nc_result: str
    problems: List[str]
