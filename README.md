# wx — GFS 气象数据下载与处理系统

GFS（Global Forecast System）数据自动下载、完整性巡检及 15 分钟时间序列生成工具集。

---

## 目录结构

```
wx/
├── gfs_fetch.py                    # 单次 GFS 数据下载（NOMADS）
├── gfs_to_15min.py                 # GRIB2 → 15 分钟时间序列 CSV（批处理）
├── gfs_guardian/                   # 下载守护进程（多区域自动巡检下载）
├── gfs_15min_guardian/             # 转换守护进程（GRIB2 → CSV 持续监控）
├── gfs_service/                    # 服务层模型
├── hist_fetch/                     # 历史数据下载（NOMADS + S3 回落）
├── gfs_repo/                       # 变量契约定义
├── gfs_guardian.service            # systemd：下载守护进程服务文件
├── gfs_to_15min_guardian.service   # systemd：转换守护进程服务文件
├── tests/                          # 单元测试 & smoke 测试
├── data_js/                        # 江苏区域 GRIB2 数据（自动生成，不入库）
├── data_sc/                        # 四川区域 GRIB2 数据（自动生成，不入库）
├── data_nx/                        # 宁夏区域 GRIB2 数据（自动生成，不入库）
├── data_js_csv/                    # 江苏 15 分钟 CSV 输出（自动生成，不入库）
├── data_sc_csv/                    # 四川 15 分钟 CSV 输出（自动生成，不入库）
├── data_nx_csv/                    # 宁夏 15 分钟 CSV 输出（自动生成，不入库）
├── gfs_15min_out/                  # 其他 15 分钟输出目录
└── logs/                           # 运行日志
```

---

## 依赖安装

```bash
pip install eccodes numpy pandas
# 可选：低延迟文件系统监听（gfs_15min_guardian 用，不装则自动降级为轮询）
pip install watchdog
# 可选：安装 wgrib2 以在 S3 下载时裁剪 bbox（否则保存全球 GRIB）
```

---

## 启动方式

### 1. 守护进程（推荐生产使用）

守护进程 `gfs_guardian` 会自动对配置的多个区域（江苏、四川、宁夏）进行历史和实时数据的巡检与补全下载。

#### 方式 A：直接运行

```bash
cd /root/my_product/wx
python -m gfs_guardian
```

- 历史窗口巡检间隔：每 10 分钟
- 实时窗口巡检间隔：每 5 分钟
- 日志输出至 `logs/gfs_guardian.log` 及控制台
- `Ctrl+C` 或 `SIGTERM` 优雅退出

#### 方式 B：systemd 托管（开机自启、自动重启）

```bash
# 安装服务
cp gfs_guardian.service /etc/systemd/system/
systemctl daemon-reload

# 启动
systemctl start gfs-guardian

# 设置开机自启
systemctl enable gfs-guardian

# 查看状态
systemctl status gfs-guardian

# 查看实时日志
journalctl -u gfs-guardian -f

# 停止
systemctl stop gfs-guardian
```

---

### 2. 单次数据下载

#### 近实时数据（NOMADS，~10 天内）

```bash
python gfs_fetch.py \
    --date  2026-02-26 \
    --cycle 00 \
    --bbox  "112.25,31.5,122.0,34.5" \
    --fh    0:23 \
    --out   ./gfs_data
```

#### 历史数据（NOMADS + S3 自动回落）

```bash
# 单天
python hist_fetch/hist_fetch.py \
    --start 2026-01-01 --end 2026-01-01 \
    --cycle 00 \
    --bbox  "112.25,31.5,122.0,34.5" \
    --fh    0:23 \
    --out   ./gfs_data

# 批量（4 天并行）
python hist_fetch/hist_fetch.py \
    --start 2026-01-01 --end 2026-01-31 \
    --cycle 00 \
    --bbox  "112.25,31.5,122.0,34.5" \
    --fh    0:23 \
    --out   ./gfs_data \
    --workers 4
```

> 超出 ~10 天的日期请使用 `hist_fetch.py`，它会在 NOMADS 不可用时自动回落至 S3。

---

### 3. GRIB2 转 15 分钟时序 CSV（批处理，一次性）

将已下载的 GRIB2 数据处理为 15 分钟间隔时间序列，输出为 CSV 文件。

```bash
# 处理指定日期
python gfs_to_15min.py \
    --data-dir ./data_js \
    --date 2026-02-26 \
    --out-dir ./data_js_csv

# 处理全部日期
python gfs_to_15min.py \
    --data-dir ./data_js \
    --all \
    --out-dir ./data_js_csv
```

---

### 4. 转换守护进程（持续监控，推荐生产使用）

`gfs_15min_guardian` 持续监控三个区域的 GRIB2 输入目录，自动将新到数据转换为 15 分钟 CSV。

#### 默认目录映射

| 区域 | 输入目录  | 输出目录      |
|------|-----------|---------------|
| 江苏 | data_js/  | data_js_csv/  |
| 四川 | data_sc/  | data_sc_csv/  |
| 宁夏 | data_nx/  | data_nx_csv/  |

> **注意：** 输出目录名为 `data_nx_csv`（不是 `data_nx_data`）。如需自定义，通过 `--mapping` 参数或 `GFS15M_MAPPING` 环境变量配置，见下文。

#### 方式 A：直接运行

```bash
cd /root/my_product/wx
python -m gfs_15min_guardian
```

常用参数：

```bash
python -m gfs_15min_guardian \
    --scan-interval 300 \    # 定时扫描间隔（秒），默认 300
    --workers 2 \             # 并发 worker 数，默认 2
    --no-watch \              # 禁用 watchdog（纯轮询模式）
    --log-dir ./logs \        # 日志目录
    --state-file ./logs/gfs_15min_state.json  # 状态文件
```

自定义目录映射（格式 `name:src:dst`，逗号分隔）：

```bash
python -m gfs_15min_guardian \
    --mapping "jiangsu:data_js:data_js_csv,sichuan:data_sc:data_sc_csv,ningxia:data_nx:data_nx_csv"
```

环境变量方式（等效）：

```bash
export GFS15M_MAPPING="jiangsu:data_js:data_js_csv,sichuan:data_sc:data_sc_csv,ningxia:data_nx:data_nx_csv"
export GFS15M_SCAN_INTERVAL=300
export GFS15M_WORKERS=2
python -m gfs_15min_guardian
```

#### 方式 B：systemd 托管

```bash
# 安装服务
cp gfs_to_15min_guardian.service /etc/systemd/system/
systemctl daemon-reload

# 启动
systemctl start gfs-15min-guardian

# 开机自启
systemctl enable gfs-15min-guardian

# 查看状态
systemctl status gfs-15min-guardian

# 查看实时日志
journalctl -u gfs-15min-guardian -f

# 停止（等待正在运行的转换任务完成，最多 120 秒）
systemctl stop gfs-15min-guardian
```

#### 工作机制

1. **启动时补扫**：立即全量扫描各输入目录，将未转换的历史日期补齐
2. **watchdog 监听**（需 `pip install watchdog`）：输入目录出现新的 init 子目录时实时触发转换
3. **定时兜底扫描**：每 `--scan-interval` 秒全量扫描一次，防止漏事件
4. **幂等保证**：
   - 已生成 CSV 且输入未变化时自动跳过
   - 同一日期不并发处理（in-flight 锁）
   - 处理结果写入状态文件 `logs/gfs_15min_state.json`

#### 排查问题

```bash
# 查看运行日志
tail -f logs/gfs_15min_guardian.log

# 查看状态文件（各日期处理结果）
cat logs/gfs_15min_state.json | python3 -m json.tool | grep -A3 '"status"'

# 强制重新处理某日期（删除对应 CSV 即可，下次扫描自动触发）
rm data_js_csv/gfs_15min_2026-02-26.csv
```

---

## 区域配置

当前守护进程覆盖以下区域（见 `gfs_guardian/config.py`）：

| 区域 | bbox（W,S,E,N）       | 输出目录  | 历史起始     |
|------|-----------------------|-----------|--------------|
| 江苏 | 115.5,30.5,122.5,35.5 | data_js/  | 2025-09-01   |
| 四川 | 96.5,25.5,109.5,35.0  | data_sc/  | 2025-09-01   |
| 宁夏 | 103.5,35.0,108.5,40.0 | data_nx/  | 2025-09-01   |

---

## 日志

| 路径 | 说明 |
|------|------|
| `logs/gfs_guardian.log` | 下载守护进程主日志（滚动，最大 50 MB × 5 个） |
| `logs/tasks/` | 各下载任务详细日志 |
| `logs/gfs_15min_guardian.log` | 转换守护进程主日志（滚动，最大 50 MB × 5 个） |
| `logs/gfs_15min_state.json` | 转换状态文件（各日期处理结果，含 mtime 快照） |

---

## 详细接口文档

见 [GFS_FETCH_API.md](GFS_FETCH_API.md)。
