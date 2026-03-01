# GFS 数据下载服务接口文档

## 概览

| 脚本 | 数据源 | 适用场景 |
|------|--------|----------|
| `gfs_fetch.py` | NOMADS filter（仅）| 近实时数据，NOMADS 窗口内（~10 天） |
| `hist_fetch/hist_fetch.py` | NOMADS → S3 自动回落 | 历史归档 / 批量 / 单天均可 |

> **推荐：** 除非明确只需要 NOMADS，统一使用 `hist_fetch.py`，它会自动处理 NOMADS 不可用的情况。

变量集合由 `gfs_repo/var_contract.py` 统一定义，两个脚本不接受 `--vars` 参数，下载内容固定。

---

## 1. gfs_fetch.py

### 命令行签名

```
python gfs_fetch.py
    --date  YYYY-MM-DD      # 必填  UTC 日期
    --cycle {00|06|12|18}   # 必填  起报时次
    --bbox  "W,S,E,N"       # 必填  经纬度范围（度）
    [--fh    START:END]     # 预报时次范围，默认 0:23
    [--out   DIR]           # 输出根目录，默认 ./gfs_data
    [--retries N]           # 单文件重试次数，默认 3
    [--timeout N]           # HTTP 超时（秒），默认 30
```

### 示例

```bash
python gfs_fetch.py \
    --date  2026-02-26 \
    --cycle 00 \
    --bbox  "112.25,31.5,122.0,34.5" \
    --fh    0:23 \
    --out   ./gfs_data
```

### 注意事项

- 若 NOMADS 返回 404（数据未发布 / 超出窗口），脚本直接报错退出，**不回落 S3**
- GFS 00Z 通常 ~05:00 UTC 可用，06Z ~11:00 UTC，依此类推
- 超出 ~10 天的日期请改用 `hist_fetch.py`

---

## 2. hist_fetch.py

### 命令行签名

```
python hist_fetch/hist_fetch.py
    --start   YYYY-MM-DD      # 必填  起始 UTC 日期（含）
    --end     YYYY-MM-DD      # 必填  结束 UTC 日期（含）
    --cycle   {00|06|12|18}   # 必填  起报时次
    --bbox    "W,S,E,N"       # 必填  经纬度范围（度）
    [--fh     START:END]      # 预报时次范围，默认 0:23
    [--out    DIR]            # 输出根目录，默认 ./gfs_data
    [--retries N]             # 单文件重试次数，默认 3
    [--timeout N]             # HTTP 超时（秒），默认 30
    [--workers N]             # 并行日期数，默认 1（顺序）
```

### 示例

```bash
# 单天
python hist_fetch/hist_fetch.py \
    --start 2026-02-27 --end 2026-02-27 \
    --cycle 00 --bbox "112.25,31.5,122.0,34.5" \
    --fh 0:23 --out ./gfs_data

# 批量（4 天并行）
python hist_fetch/hist_fetch.py \
    --start 2026-01-01 --end 2026-01-31 \
    --cycle 00 --bbox "112.25,31.5,122.0,34.5" \
    --fh 0:23 --out ./gfs_data --workers 4
```

### 注意事项

- 每个日期先尝试 NOMADS；收到 403/404 自动切换 S3 byte-range 下载
- S3 下载时若系统路径中有 `wgrib2` / `wgrib2.exe`，自动裁剪至 bbox；否则保存全球 GRIB
- `--workers` 建议 ≤ 4，过大会触发 S3 限流
- `--fh 0:0` 可只取分析场（注意 `tp`/`dswrf`/`dlwrf` 等累积量在 f000 为空）

---

## 3. 输出目录结构

```
{out}/
└── init=20260226T0000Z/          # 每个起报时次一个子目录
    ├── gfs.t00z.pgrb2.0p25.f000  # GRIB2 文件，按预报时次命名
    ├── gfs.t00z.pgrb2.0p25.f001
    ├── ...
    └── manifest.json             # 本次下载元数据
```

### manifest.json 结构

```json
{
  "init_time":      "20260226T0000Z",
  "date_utc":       "20260226",
  "cycle":          "00",
  "bbox":           {"west": 112.25, "south": 31.5, "east": 122.0, "north": 34.5},
  "fh_range":       {"start": 0, "end": 23},
  "vars":           ["t2m", "d2m", "rh2m", ...],
  "files":          [{"name": "gfs.t00z.pgrb2.0p25.f000", "fh": 0, "size": 153600}],
  "created_at_utc": "2026-02-26T06:12:34.000000+00:00"
}
```

---

## 4. 变量契约（var_contract.py）

所有下载变量固定，共 **28 个**：

| 规范键 | GRIB2 变量 | 层次 | 物理含义 |
|--------|-----------|------|---------|
| `t2m` | TMP | 2 m above ground | 2 米气温 |
| `d2m` | DPT | 2 m above ground | 2 米露点温度 |
| `rh2m` | RH | 2 m above ground | 2 米相对湿度 |
| `u10` | UGRD | 10 m above ground | 10 米 U 风 |
| `v10` | VGRD | 10 m above ground | 10 米 V 风 |
| `u100m` | UGRD | 100 m above ground | 100 米 U 风 |
| `v100m` | VGRD | 100 m above ground | 100 米 V 风 |
| `u925` | UGRD | 925 mb | 925 hPa U 风 |
| `v925` | VGRD | 925 mb | 925 hPa V 风 |
| `u850` | UGRD | 850 mb | 850 hPa U 风 |
| `v850` | VGRD | 850 mb | 850 hPa V 风 |
| `u700` | UGRD | 700 mb | 700 hPa U 风 |
| `v700` | VGRD | 700 mb | 700 hPa V 风 |
| `u500` | UGRD | 500 mb | 500 hPa U 风 |
| `v500` | VGRD | 500 mb | 500 hPa V 风 |
| `gust10m` | GUST | surface | 10 米阵风 |
| `psfc` | PRES | surface | 地面气压 |
| `mslp` | PRMSL | mean sea level | 海平面气压 |
| `hpbl` | HPBL | surface | 行星边界层高度 |
| `tcc` | TCDC | entire atmosphere | 总云量 |
| `tp` | APCP | surface | 累积降水量 ⚠️ |
| `prate` | PRATE | surface | 瞬时降水率 |
| `pwat` | PWAT | entire atmosphere (considered as a single layer) | 可降水量 |
| `cape` | CAPE | surface | 对流有效位能 |
| `dswrf` | DSWRF | surface | 向下短波辐射 ⚠️ |
| `dlwrf` | DLWRF | surface | 向下长波辐射 ⚠️ |
| `land` | LAND | surface | 陆地掩膜 |
| `snod` | SNOD | surface | 雪深 |

> ⚠️ `tp` / `dswrf` / `dlwrf` 为累积场，**f000（分析时刻）不含该字段**，从 f001 起有值。

---

## 5. 服务调用方式

### 5.1 subprocess 调用（推荐，语言无关）

```python
import subprocess, sys

result = subprocess.run(
    [
        sys.executable,
        "hist_fetch/hist_fetch.py",
        "--start",   "2026-02-27",
        "--end",     "2026-02-27",
        "--cycle",   "00",
        "--bbox",    "112.25,31.5,122.0,34.5",
        "--fh",      "0:23",
        "--out",     "./gfs_data",
        "--workers", "2",
    ],
    capture_output=True,
    text=True,
)
if result.returncode != 0:
    raise RuntimeError(f"hist_fetch failed:\n{result.stderr}")
```

### 5.2 Python 直接 import

```python
from pathlib import Path
from hist_fetch.hist_fetch import run
from datetime import date

run(
    start    = date(2026, 2, 27),
    end      = date(2026, 2, 27),
    cycle    = "00",
    bbox     = {"west": 112.25, "south": 31.5, "east": 122.0, "north": 34.5},
    out      = Path("./gfs_data"),
    fh_start = 0,
    fh_end   = 23,
    retries  = 3,
    timeout  = 30,
    workers  = 1,
)
```

```python
# gfs_fetch.py
import sys
sys.path.insert(0, "/path/to/wx")   # 根目录加入 path
from gfs_fetch import fetch
from pathlib import Path

fetch(
    date     = "2026-02-26",
    cycle    = "00",
    bbox     = {"west": 112.25, "south": 31.5, "east": 122.0, "north": 34.5},
    out      = Path("./gfs_data"),
    fh_start = 0,
    fh_end   = 23,
    retries  = 3,
    timeout  = 30,
)
```

### 5.3 读取 manifest 判断完成状态

```python
import json
from pathlib import Path

manifest_path = Path("gfs_data/init=20260226T0000Z/manifest.json")
manifest = json.loads(manifest_path.read_text())

print(manifest["init_time"])          # "20260226T0000Z"
print(manifest["vars"])               # ["t2m", "d2m", ...]
print(len(manifest["files"]))         # 24（fh 0-23）
for f in manifest["files"]:
    print(f["name"], f["fh"], f["size"])
```

---

## 6. 退出码

| 退出码 | 含义 |
|--------|------|
| `0` | 所有文件下载成功 |
| `1` | 至少一个日期/文件失败（`hist_fetch.py`），或单文件重试耗尽（`gfs_fetch.py`） |

---

## 7. 已知限制

| 限制 | 说明 |
|------|------|
| NOMADS 窗口 | 近 ~10 天，超出请用 `hist_fetch.py` |
| S3 wgrib2 裁剪 | 无 `wgrib2` 时保存全球 GRIB（文件更大） |
| 累积场 f000 | `tp` / `dswrf` / `dlwrf` 在 f000 无数据 |
| NOMADS level bleed | NOMADS 下载文件会含 15 个额外 GRIB 消息（同层其他变量），不影响契约字段完整性 |
| 变量不可自定义 | `--vars` 参数不存在，变量集由 `var_contract.py` 统一管理 |
