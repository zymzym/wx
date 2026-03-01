from __future__ import annotations
from typing import Optional
from pydantic import BaseModel, field_validator


class TaskCreate(BaseModel):
    start_date: str          # "2026-01-01"
    end_date:   str          # "2026-01-31"
    cycle:      str          # "00" | "06" | "12" | "18"
    bbox:       str          # "112.25,31.5,122.0,34.5"  (W,S,E,N)
    fh:         str  = "0:23"
    workers:    int  = 1
    out:        str  = "./gfs_data"

    @field_validator("cycle")
    @classmethod
    def check_cycle(cls, v: str) -> str:
        if v not in {"00", "06", "12", "18"}:
            raise ValueError("cycle must be 00 / 06 / 12 / 18")
        return v

    @field_validator("bbox")
    @classmethod
    def check_bbox(cls, v: str) -> str:
        parts = v.split(",")
        if len(parts) != 4:
            raise ValueError("bbox must be 'W,S,E,N'")
        try:
            [float(p) for p in parts]
        except ValueError:
            raise ValueError("bbox values must be numbers")
        return v


class TaskOut(BaseModel):
    id:                 str
    status:             str        # pending | running | done | failed
    created_at:         str
    updated_at:         str
    start_date:         str
    end_date:           str
    cycle:              str
    bbox:               str
    fh:                 str
    workers:            int
    out_dir:            str
    log_path:           str
    pid:                Optional[int]
    exit_code:          Optional[int]
    error:              Optional[str]
    manifests_found:    int
    manifests_expected: int
