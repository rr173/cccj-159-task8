# 光罩投片交付中心 Mask Tape-out Delivery Center

接收某次芯片发行的 **GDS/OASIS、层映射、die 尺寸、scribe line、工艺角与晶圆厂 recipe**，
生成**可追溯的 mask set**，并编排曝光窗口与 wafer lot；提供浏览器界面、HTTP 接口与命令行。

**零第三方依赖**：Python 3.11 标准库（`http.server` + `sqlite3` + `tarfile`）+ 原生 JS。

## 运行

```bash
# HTTP + 浏览器界面（默认 127.0.0.1:8080，数据目录 ./mdc-data）
python -m mdc.cli --db ./mdc-data serve --host 0.0.0.0 --port 8080
# 浏览器打开 http://localhost:8080 ，右上角选择操作自然人

# 命令行（与 HTTP 共享同一个 SQLite 库，可同时使用）
python -m mdc.cli --db ./mdc-data releases
python -m mdc.cli --db ./mdc-data sets
python -m mdc.cli --db ./mdc-data windows
```

数据目录结构：`maskcenter.db`（全部业务状态、审计）、`blobs/`（内容寻址 CAS，
`xx/yy/<sha256>`）、`packages/`（只读交付包 `.tar`）。

## 端到端流程

```
上传资料(摘要校验/CAS去重)
      │  同名不同内容 → quarantine 隔离等待处置
      ▼
导入发行(逐构件校验 + 规范化)
      │  完整且一致 → 稳定交付编号 MS-… + MASTER mask set
      ▼
登记曝光机/wafer lot → 预订曝光窗口（机器时间排他/先后次序/lot 单一绑定）
      │
      ├─ 尚未曝光：ME + STEWARD 两名不同自然人批准 → 替换单构件
      │            → 生成 REVISION 谱系节点，窗口/lot 重算到新节点
      └─ 任一窗口曝光：原节点永久封存（SEALED）
                       → 只能创建 DELTA set + 返工计划
      ▼
导出晶圆厂只读交付包（MANIFEST + 构件，确定性 tar，0444，下载再校验）
```

## 核心机制

### 稳定交付编号与去重
- 规范化：导入包字段按角色排序后做确定性 JSON（`sort_keys`、紧凑分隔符）→ SHA-256。
- 交付编号 = `MS-` + 16 位 base32hex，**同一资料永远同一编号**；重复导入返回
  首次结果（`replayed:true`），构件实体按内容寻址（CAS），**不复制任何字节**。
- 条目顺序、空白差异不影响编号。

### 导入校验（每个不一致都定位到具体构件）
| 原因码 | 含义 |
|---|---|
| `DIGEST_MISMATCH` / `DIGEST_TAMPERED` | 上传声明摘要不符 / 已存内容重算摘要不符（篡改） |
| `ARTIFACT_NOT_REGISTERED` / `ARTIFACT_QUARANTINED` | 引用了未登记或隔离中的构件 |
| `MISSING_LAYOUT` / `MISSING_LAYERMAP` / `MISSING_RECIPE` / `MISSING_MASK_FILE` | 缺层/缺构件 |
| `RECIPE_MISMATCH` / `CORNER_NOT_IN_RECIPE` | recipe_id/fab/工艺角对应关系不符 |
| `DEPENDENCY_INVERSION` / `MISSING_MASTER` | 主版→修正版→派生层次序倒置、缺上游主版、依赖成环 |
| `LAYERMAP_MASK_NOT_IN_RECIPE` / `MISSING_LAYER_MAPPING` / `MASK_LAYER_MISMATCH` | 层映射与 recipe 对应不符 |
| `LAYERMAP_LAYER_NOT_IN_LAYOUT` | 映射的层在 GDS/OASIS 实扫清单中不存在 |
| `INVENTORY_MISMATCH` / `LAYOUT_INVENTORY_MISSING` | 声明层清单与实扫不符 / OASIS 未随附清单 |
| `RELEASE_NAME_COLLISION` | 同名异物（同发行名、不同内容）→ 隔离等待处置 |

GDSII 会被真正解析（最小记录流扫描）提取 `L<layer>.<datatype>`；OASIS 因压缩流
要求随附 `layer_inventory`。每条 finding 带 `locator`（`type/id/role/layer/mask…`）。

### 资源仲裁与幂等操作编号
- 所有写操作可携带客户端**原操作编号**（HTTP `X-Op-Id`，CLI `--op`）。
- 两名调度员并发抢占同一曝光机时间段：`BEGIN IMMEDIATE` 串行裁决，
  **先到唯一赢家**，输家得 `WINDOW_OVERLAP` 并在 `arbitration` 表留下赢家操作编号。
- 输家用**同一编号安全重试**：回放首次结论（`detail.replayed=true`），不会产生第二个窗口；
  赢家重试拿到同一窗口。同编号不同载荷 → `OP_PAYLOAD_CONFLICT`；过期 → `OP_EXPIRED`。
- 区间相交判定为半开区间 `[start,end)`：端点相接不算重叠。

### 先后次序与封存
- 曝光执行时校验：窗口 lot 必须绑定该 set；该 mask 的每个前置 mask 必须已在同 lot 曝光，
  否则 `DEPENDENCY_NOT_EXPOSED`。
- 任一窗口首次曝光：mask set 节点转 `SEALED`（`sealed_at` 留痕）、lot 转 `EXPOSED`。
- 封存后：新窗口/原地替换一律 `SET_SEALED_INPLACE`；只能创建 **DELTA set** 与返工计划。

### 双角色替换（曝光前）
- 申请人与批准人为**制造工程师 ME** 与**版图负责人 STEWARD**；两签必须由
  **两个不同自然人**完成（同一自然人即使兼两职也不行 → `APPROVAL_SAME_PERSON`），
  越权 → `UNAUTHORIZED`。
- 第二签生效：生成 `REVISION` 新谱系节点（同 lineage，seq+1），旧节点 `SUPERSEDED`，
  未曝光窗口与 lot 绑定原子迁移到新节点（下游排程重算）。替换构件会先做层适用性与摘要校验。

### 实体版匣保管交接链

库房为每个实体掩模版匣登记防拆标签、柜位和当前责任人，操作页为 `/custody`（单页中的
"版匣保管"标签，浏览器直达 `/#custody`），API 前缀 `/api/custody`。

- **双方确认**：出库为 `WAREHOUSE → LINE`，入库为 `LINE → WAREHOUSE`；交出者与接收者
  分别确认，每笔交接只认一个接收者。创建申请时可不指定接收者，此时第一个具有接收职责的
  自然人以条件更新原子锁定接收权；其他竞争者得到 `RECEIVER_MISMATCH`。
- **申请留痕与冻结**：标签号码错误（`LABEL_MISMATCH`）、人员缺少职责
  （`PERSON_ROLE_INVALID`）、版匣已有未决交接（`POD_FROZEN`）或确认期限已过
  （`CONFIRMATION_DEADLINE_PASSED`）都会留下 `REJECTED/EXPIRED` 申请和明确原因；
  PENDING 交接冻结版匣，未双方确认前曝光门禁返回 `POD_NOT_ISSUED`。
- **请求幂等**：保管类操作使用长期有效的原操作编号（`X-Op-Id`），同编号同载荷重放
  第一次结果；一方确认后不能替换交出者或接收者，晚到确认、再次撤销均不能推翻终态。
- **撤销**：只有申请人或库房管理员可在完成前撤销；撤销清除冻结并恢复原责任人。
- **入库检查/调查**：入库完成后版匣处于 `AWAITING_INSPECTION`。标签完好转入库位；
  标签破损则进入 `INVESTIGATING`，禁止再次出库但历次保管关系仍完整可查。
- **解除调查**：破损标签必须先由库房管理员更换；解除需安全员（SAFETY）与工艺负责人
  （PROCESS）由不同自然人确认，然后恢复 `IN_STORAGE`。
- **持久化与重启**：申请、确认、撤销、检查、换标和调查均写入 SQLite；启动时接续未过期
  PENDING 待办，自动过期超期交接并清除冻结。

预置保管角色：`u_wh1/u_wh2`（库房）、`u_line1/u_line2`（生产线）、`u_safe`（安全员）、
`u_proc`（工艺负责人）。

## 持久化与崩溃恢复
- 资料、占用、批准、谱系、仲裁、交付包、操作日志全部在 SQLite（WAL、`synchronous=FULL`）。
- 交付包构建采用**先落 BUILDING 状态行、再写文件**；进程重启时 `Service.__init__`
  自动把 BUILDING 包**幂等续建**为 READY（tar 确定性：mtime=0、mode=0444、无 owner），
  并把崩溃时未决且已过期的操作编号置为 `EXPIRED`。
- 交付包文件 `0444`，下载时重新计算文件 SHA-256，与登记不符 → `PACKAGE_TAMPERED`。

## 主要原因码（明确拒绝）

| 场景 | 原因码 |
|---|---|
| 摘要被篡改 | `DIGEST_MISMATCH` / `DIGEST_TAMPERED` / `PACKAGE_TAMPERED` |
| 缺层/缺构件 | `MISSING_*` |
| 依赖倒置 | `DEPENDENCY_INVERSION` / `DEPENDENCY_NOT_EXPOSED` |
| 窗口重叠 | `WINDOW_OVERLAP`（仲裁唯一赢家） |
| 过期操作编号 | `OP_EXPIRED`（载荷冲突 `OP_PAYLOAD_CONFLICT`，裁决中 `OP_PENDING`） |
| 越权替换 | `UNAUTHORIZED` / `APPROVAL_SAME_PERSON` |
| 曝光后原地改写 | `SET_SEALED_INPLACE`（只能 delta + 返工） |
| lot 绑定冲突 | `LOT_ALREADY_BOUND` 语义由 `LOT_WINDOWS_PENDING` / `DELTA_LINEAGE_MISMATCH` 表达 |

## 预置自然人

| ID | 角色 |
|---|---|
| `u_mei` | 制造工程师 ME |
| `u_tao` | 版图负责人 STEWARD |
| `u_disp1` / `u_disp2` | 两名调度员 DISPATCHER |
| `u_wh1` / `u_wh2` | 库房管理员/保管员 WAREHOUSE（u_wh1 兼 WAREHOUSE_ADMIN） |
| `u_line1` / `u_line2` | 生产线接收员 LINE |
| `u_safe` / `u_proc` | 安全员 SAFETY / 工艺负责人 PROCESS |
| `u_admin` | 管理员 ADMIN（隔离处置、账号管理） |

## 测试

```bash
python -m unittest discover -s tests -t .
```

覆盖：导入规范化/去重、全部拒收定位、同名隔离处置、并发抢占唯一赢家、
操作编号安全重试/过期/载荷冲突、曝光次序、双签不同自然人、谱系封存、
delta+返工跨谱系拒绝、交付包确定性/篡改/崩溃续建、实体版匣双方保管/竞争接收/
检查调查/重启恢复、HTTP 全流程。
