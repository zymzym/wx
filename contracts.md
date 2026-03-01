
1. **核心数据模型**（Signature / InitTime / Partition）
    
2. **API 列表与语义**（必备 + 推荐）
    
3. **错误码与幂等规则**（保证“不出错”）
    

---

## 0) 核心约定

- **唯一分区键**：`init_time`（如 `20260226T0000Z`）
    
- **signature**：决定“这是什么数据”（model/grid/step/source/bbox/varset/var_keys）
    
- **业务读取只允许读取** `status=complete` 且存在 `_SUCCESS` 的分区
    

---

## 1) 数据模型

### 1.1 Signature

```json
{
  "model": "gfs",
  "grid": "0p25",
  "step": "1hr",
  "source": "nomads_filter",
  "bbox": {"north": 34.5, "west": 112.25, "south": 31.5, "east": 122.0},
  "varset": "v1",
  "var_keys": ["t2m","d2m","rh2m","u10","v10","..."]
}
```

### 1.2 InitTime

- 字符串：`YYYYMMDDTHH00Z`
    
- 解析后等价：`init_date` + `cycle(00/06/12/18)`

### 1.3 PartitionSpec

```json
{
  "signature": { "...": "..." },
  "init_time": "20260226T0000Z",
  "forecast_hours": {"start": 0, "end": 120}
}
```

### 1.4 PartitionStatus（枚举）

- `missing`：分区不存在
    
- `incomplete`：存在但不完整（缺文件/无manifest/无_SUCCESS/校验失败）
    
- `complete`：完整可用
    
- `quarantined`：被隔离（原因可查）
    

---

## 2) API 契约（必备）

> 
> - today(UTC0) 用最新
>     
> - today 之前补齐 00Z
>     

### 2.1 `repo.status(spec) -> status + detail`

**用途**：判定分区是否可用、为什么不可用  
**输入**：PartitionSpec  
**输出**：

```json
{
  "status": "complete",
  "paths": {"raw": "...", "nc": "..."},
  "detail": {"missing_fh": [], "bad_files": []}
}
```

### 2.2 `repo.verify(spec, dataset="raw|nc", strict=true) -> VerifyReport`

**用途**：做“可用性判定”的权威函数（业务层只信它）  
**最低校验**：

- manifest.json 可读
    
- fh 覆盖范围完整（start..end）
    
- 每个文件 bytes > min_bytes
    
- `_SUCCESS` 存在（complete 必须有）
    
- strict=true 时：sha256 一致
    

**输出**：

```json
{
  "ok": true,
  "status": "complete",
  "problems": []
}
```

### 2.3 `repo.put_raw(spec, downloader, policy) -> PutResult`

**用途**：下载并原子提交 raw(grib2) 分区  
**强制语义**：

- 原子写入：tmp -> 校验 -> move -> 写 _SUCCESS
    
- 幂等：如果已 complete，则直接返回 `already_complete=true`
    
- 若存在但 incomplete：按 policy 处理（隔离/覆盖）
    

**输出**：

```json
{
  "result": "created|updated|skipped",
  "already_complete": false,
  "raw_path": ".../raw/.../init=...",
  "manifest_path": ".../manifest.json"
}
```

### 2.4 `repo.put_nc(spec, converter, policy) -> PutResult`

**用途**：将 raw 转 nc 并提交 nc 分区  
**前置**：raw 分区必须 complete  
**语义同 put_raw**（tmp + 原子提交 + 幂等）

### 2.5 `repo.quarantine(spec, reason, dataset="raw|nc|both") -> QuarantineResult`

**用途**：把脏分区隔离（推荐默认，而不是 rm -rf）  
**输出**：

```json
{
  "moved": true,
  "src": "...",
  "dst": ".../quarantine/reason=.../..."
}
```

### 2.6 `repo.resolve_init_time(signature, init_date_utc, strategy) -> ResolveResult`

**用途**：把你的业务规则固化成“唯一入口”，上层不用思考 cycle

- strategy = `today_latest`：对 init_date == today_utc0
    
- strategy = `history_canonical_00`：对 init_date < today_utc0（只选 00Z）
    

**输出**：

```json
{
  "chosen_init_time": "20260226T1800Z",
  "reason": "today_latest_complete",
  "candidates_tried": ["20260226T1800Z","20260226T1200Z","20260226T0600Z","20260226T0000Z"]
}
```

---

## 3) API 契约（推荐增强）

### 3.1 `repo.list_inits(signature, init_date_utc=None) -> [InitInfo]`

用途：列出某天有哪些 cycle 分区、各自状态  
返回例：

```json
[
  {"init_time":"20260226T0000Z","status":"complete"},
  {"init_time":"20260226T1800Z","status":"incomplete"}
]
```

### 3.2 `repo.ensure(spec, ensure_raw=true, ensure_nc=false, policy) -> EnsureResult`

用途：一键“确保存在并完整”（缺就补、脏就修）  
这是最适合 Agent 调度的 API。

### 3.3 `repo.read_manifest(spec, dataset="raw|nc") -> Manifest`

用途：统一读元数据（bbox/varset/var_keys/文件清单）

### 3.4 `repo.gc(signature, older_than_days, keep_rules) -> GCReport`

用途：清理旧数据（但保留 canonical 00Z 或保留最近 N 天）

---

## 4) 错误码与幂等规则（不出错的关键）

### 4.1 错误码（建议枚举）

- `E_INVALID_SIGNATURE`：signature 缺字段/varset 不存在
    
- `E_INVALID_INIT_TIME`：init_time 格式错误
    
- `E_LOCKED`：分区被锁（并发下载中）
    
- `E_DOWNLOAD_HTTP`：下载 HTTP 非 200
    
- `E_DOWNLOAD_HTML`：返回 text/html（错误页）
    
- `E_VERIFY_FAILED`：校验失败（缺文件/大小不对/sha 不对）
    
- `E_RAW_MISSING`：转 nc 前 raw 不存在或不 complete
    

### 4.2 幂等规则（强制）

- `put_raw/put_nc`：
    
    - 若目标分区 `complete` → 必须返回 `skipped/already_complete`
        
    - 若 `incomplete`：
        
        - policy=quarantine → 先 quarantine 再重建
            
        - policy=overwrite → 覆盖重建（仍需原子提交）
            
- `verify`：只读，不改变状态
    
- `resolve_init_time`：只返回“该选哪个”，不做下载（下载由 ensure/put 执行）
    

---

## 5) 把你的两条规则直接落到 API 调用序列

### 5.1 历史补齐（init_date < today_utc0）

```text
spec = {signature, init_time = init_date@00Z, fh_range}
repo.ensure(spec, ensure_raw=true, ensure_nc=需要则true)
```

### 5.2 今日最新（init_date == today_utc0）

```text
r = repo.resolve_init_time(signature, today_utc0, strategy="today_latest")
spec = {signature, init_time=r.chosen_init_time, fh_range}
repo.ensure(spec, ensure_raw=true, ensure_nc=需要则true)
```

---

## 6) 最简“接口清单”汇总（你可以直接贴进文档）

```yaml
repository_api:
  - status(spec) -> status, detail
  - verify(spec, dataset, strict) -> report
  - put_raw(spec, downloader, policy) -> result
  - put_nc(spec, converter, policy) -> result
  - quarantine(spec, reason, dataset) -> result
  - resolve_init_time(signature, init_date_utc, strategy) -> chosen_init
  - ensure(spec, ensure_raw, ensure_nc, policy) -> result   # 推荐
  - list_inits(signature, init_date_utc) -> list            # 推荐
  - read_manifest(spec, dataset) -> manifest                # 推荐
```

---

## 7) 机器可读规范（用于直接驱动代码生成）

### 7.1 字段命名与格式

- 所有键使用小写蛇形或小写无分隔：`model/grid/step/source/init_time/forecast_hours/varset/var_keys`
- 时间字符串 `init_time` 格式：`YYYYMMDDTHHMMZ`，仅允许 `HHMM ∈ {0000, 0600, 1200, 1800}`
- 路径规范：`{root}/{dataset}/{model}/{grid}/{step}/{source}/init={init_time}`

### 7.2 JSON Schema（核心类型）

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.com/schemas/gfs-repo.json",
  "title": "GFS Repository Types",
  "type": "object",
  "definitions": {
    "BBox": {
      "type": "object",
      "required": ["north", "west", "south", "east"],
      "properties": {
        "north": { "type": "number" },
        "west":  { "type": "number" },
        "south": { "type": "number" },
        "east":  { "type": "number" }
      },
      "additionalProperties": false
    },
    "Signature": {
      "type": "object",
      "required": ["model", "grid", "step", "source", "bbox", "varset", "var_keys"],
      "properties": {
        "model": { "type": "string" },
        "grid": { "type": "string" },
        "step": { "type": "string" },
        "source": { "type": "string" },
        "bbox": { "$ref": "#/definitions/BBox" },
        "varset": { "type": "string" },
        "var_keys": { "type": "array", "items": { "type": "string" }, "minItems": 1 }
      },
      "additionalProperties": false
    },
    "PartitionStatus": {
      "type": "string",
      "enum": ["missing", "incomplete", "complete", "quarantined"]
    },
    "ForecastHours": {
      "type": "object",
      "required": ["start", "end"],
      "properties": {
        "start": { "type": "integer", "minimum": 0 },
        "end": { "type": "integer", "minimum": 0 }
      },
      "additionalProperties": false
    },
    "PartitionSpec": {
      "type": "object",
      "required": ["signature", "init_time", "forecast_hours"],
      "properties": {
        "signature": { "$ref": "#/definitions/Signature" },
        "init_time": { "type": "string", "pattern": "^[0-9]{8}T(0000|0600|1200|1800)Z$" },
        "forecast_hours": { "$ref": "#/definitions/ForecastHours" }
      },
      "additionalProperties": false
    },
    "VerifyReport": {
      "type": "object",
      "required": ["ok", "status", "problems"],
      "properties": {
        "ok": { "type": "boolean" },
        "status": { "$ref": "#/definitions/PartitionStatus" },
        "problems": { "type": "array", "items": { "type": "string" } }
      },
      "additionalProperties": false
    },
    "PutResult": {
      "type": "object",
      "required": ["result", "already_complete", "raw_path", "manifest_path"],
      "properties": {
        "result": { "type": "string", "enum": ["created", "updated", "skipped"] },
        "already_complete": { "type": "boolean" },
        "raw_path": { "type": "string" },
        "manifest_path": { "type": "string" }
      },
      "additionalProperties": false
    },
    "QuarantineResult": {
      "type": "object",
      "required": ["moved", "src", "dst"],
      "properties": {
        "moved": { "type": "boolean" },
        "src": { "type": "string" },
        "dst": { "type": "string" }
      },
      "additionalProperties": false
    },
    "ResolveResult": {
      "type": "object",
      "required": ["chosen_init_time", "reason", "candidates_tried"],
      "properties": {
        "chosen_init_time": { "type": ["string", "null"], "pattern": "^[0-9]{8}T(0000|0600|1200|1800)Z$" },
        "reason": { "type": "string" },
        "candidates_tried": { "type": "array", "items": { "type": "string" } }
      },
      "additionalProperties": false
    },
    "EnsureResult": {
      "type": "object",
      "required": ["status", "raw_result", "nc_result", "problems"],
      "properties": {
        "status": { "$ref": "#/definitions/PartitionStatus" },
        "raw_result": { "type": "string" },
        "nc_result": { "type": "string" },
        "problems": { "type": "array", "items": { "type": "string" } }
      },
      "additionalProperties": false
    },
    "ManifestFile": {
      "type": "object",
      "required": ["name", "fh", "size"],
      "properties": {
        "name": { "type": "string" },
        "fh": { "type": ["integer", "string"] },
        "size": { "type": "integer", "minimum": 0 },
        "sha256": { "type": "string" }
      },
      "additionalProperties": false
    },
    "Manifest": {
      "type": "object",
      "required": ["signature", "init_time", "files", "created_at"],
      "properties": {
        "signature": { "$ref": "#/definitions/Signature" },
        "init_time": { "type": "string", "pattern": "^[0-9]{8}T(0000|0600|1200|1800)Z$" },
        "files": { "type": "array", "items": { "$ref": "#/definitions/ManifestFile" }, "minItems": 1 },
        "created_at": { "type": "string", "format": "date-time" }
      },
      "additionalProperties": false
    }
  }
}
```

### 7.3 Service 接口（方法签名与 I/O）

```yaml
service: GFSRepository
types:
  - BBox
  - Signature
  - PartitionSpec
  - PartitionStatus
  - VerifyReport
  - PutResult
  - QuarantineResult
  - ResolveResult
  - EnsureResult
  - Manifest
methods:
  status:
    params:
      spec: PartitionSpec
    returns:
      status: PartitionStatus
      paths:
        raw: string
        nc: string
      detail:
        missing_fh: string[]
        bad_files: string[]
  verify:
    params:
      spec: PartitionSpec
      dataset: { enum: ["raw","nc"] }
      strict: boolean
    returns: VerifyReport
  put_raw:
    params:
      spec: PartitionSpec
      downloader: function   # 由调用方实现
      policy: { enum: ["quarantine","overwrite"] }
    returns: PutResult
  put_nc:
    params:
      spec: PartitionSpec
      converter: function    # 由调用方实现
      policy: { enum: ["quarantine","overwrite"] }
    returns: PutResult
  quarantine:
    params:
      spec: PartitionSpec
      reason: string
      dataset: { enum: ["raw","nc","both"] }
    returns: QuarantineResult
  resolve_init_time:
    params:
      signature: Signature
      init_date_utc: date    # UTC 日期
      strategy: { enum: ["today_latest","history_canonical_00"] }
    returns: ResolveResult
  ensure:
    params:
      spec: PartitionSpec
      ensure_raw: boolean
      ensure_nc: boolean
      policy: { enum: ["quarantine","overwrite"] }
    returns: EnsureResult
  list_inits:
    params:
      signature: Signature
      init_date_utc: date?
    returns:
      - init_time: string
        status: PartitionStatus
  read_manifest:
    params:
      spec: PartitionSpec
      dataset: { enum: ["raw","nc"] }
    returns: Manifest
```

### 7.4 代码生成提示

- 所有枚举按字符串实现，保持与 JSON Schema 的枚举一致
- 读取/写入路径必须遵循存储路径规范并写 `_SUCCESS` 标记
- `verify(strict=true)` 必须校验 `sha256`，否则按大小与覆盖范围校验
- `quarantine` 返回字段名为 `src/dst`（不要使用 from/to）
