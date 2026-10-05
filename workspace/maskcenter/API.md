# HTTP 接口契约

基础路径 `http://<host>:8080`，除内容上传与包下载外全部 JSON。
错误统一：

```json
{ "error": "CODE", "message": "可读说明", "detail": { "...": "定位信息" } }
```

## 通用头

| 头 | 说明 |
|---|---|
| `X-Person-Id` | 操作自然人 id（`u_mei/u_tao/u_disp1/u_disp2/u_admin/u_keeper/u_phys1/u_phys2/u_safety/u_proc`），缺失 → 401 `UNAUTHORIZED` |
| `X-Op-Id` | 可选，**原操作编号**；同编号安全重试，冲突/过期见下 |

操作编号语义：同编号+同载荷已提交 → 回放首次响应（`replayed:true`）；
同编号不同载荷 → 409 `OP_PAYLOAD_CONFLICT`；裁决中 → 409 `OP_PENDING`；
过期 → 410 `OP_EXPIRED`。失败裁决（如抢占输家）未过期内重放同一结论
（`detail.replayed=true`）。

## 内容（构件）

### POST /api/content
原始字节上传（非 JSON）。请求头：`X-Logical-Name`、`X-Kind`（GDS/OASIS/LAYERMAP/RECIPE/OTHER）、
可选 `X-Sha256`（不符 → 422 `DIGEST_MISMATCH`）。
返回 `{sha256,size,logical_name,dedup,quarantined[,quarantine_id]}`。
同内容 → `dedup:true`；同名不同内容 → `quarantined:true` 并进入隔离区。

### GET /api/artifacts
已登记构件（含实时 CAS 摘要核验 `verified`）。

### GET /api/artifacts/{sha256}?name=
构件关系：别名、所属发行（角色/交付编号）、所在 mask set 成员关系——可追溯链路。

## 发行导入

### POST /api/imports
```json
{
  "release_name": "CHIP-A", "corner": "TT_0p90_25C", "fab": "FAB12",
  "foundry_recipe_id": "FAB12-RCP-7",
  "die": {"width_um": 5000, "height_um": 3000}, "scribe_um": 80,
  "entries": [
    {"role": "GDS",      "logical_name": "a.gds",        "sha256": "…"},
    {"role": "OASIS",    "logical_name": "a.oas",        "sha256": "…",
     "layer_inventory": ["L100.0"]},
    {"role": "LAYERMAP", "logical_name": "a.layers.json","sha256": "…"},
    {"role": "RECIPE",   "logical_name": "rcp.json",     "sha256": "…"},
    {"role": "MASK:M_MAIN", "logical_name": "m.gds",     "sha256": "…"}
  ]
}
```
GDS 与 OASIS 二选一；OASIS 必须随附 `layer_inventory`。
返回 `{release_id,status: VALID|INVALID|QUARANTINED,delivery_no,replayed,
error_count,warning_count,findings[]}`，每条 finding：
`{severity:error|warning,code,message,locator}`。

### POST /api/demo-packet
合成一份完整合法资料（真 GDSII + 层映射 + recipe），返回可直接 POST 到 /api/imports 的 packet。

### GET /api/releases · GET /api/releases/{id}
发行列表 / 详情（含构件角色、die/scribe/corner、全部核验结论）。

## Mask set / 谱系

### GET /api/sets?lineage=
谱系节点列表（MASTER/REVISION/DELTA，ACTIVE/SUPERSEDED/SEALED）。

### GET /api/sets/{id 或 MS-编号}
节点详情：`order`（曝光先后次序）、`members`（mask/角色/层/构件 sha）、
`edges`（依赖边与原因）、封存时间。

## 曝光机 / 窗口 / lot

### POST /api/tools · GET /api/tools
`{name}`（DISPATCHER）。

### POST /api/lots · GET /api/lots
`{display_name}`（DISPATCHER）。

### POST /api/lots/{id}/bind
`{set}`：同一 lot 绑定一个当前 set；有未完成窗口时 `LOT_WINDOWS_PENDING`；
已曝光 lot 仅允许绑定同谱系 DELTA（`DELTA_LINEAGE_MISMATCH`/`LOT_EXPOSED`）。

### POST /api/windows
`{tool_id,set,mask,start_ts,end_ts,lot_id?}`（ISO-8601 UTC，半开区间）。
- mask 不在 set → `MASK_NOT_IN_SET`；时间倒置 → `BAD_TIMESTAMP`；
- 同机时间重叠 → 409 `WINDOW_OVERLAP`，`detail.winner_op_id` 给出唯一赢家；
- 封存/被取代节点 → `SET_SEALED_INPLACE` / `SET_SUPERSEDED`。

### POST /api/windows/{id}/expose
执行曝光。lot 未绑定该 set → `LOT_NOT_BOUND`；前置 mask 未曝光 →
409 `DEPENDENCY_NOT_EXPOSED`（`detail.requires`）。首次曝光把节点置 `SEALED`。

### POST /api/windows/{id}/cancel
取消 RESERVED 窗口（已曝光不可取消）。

### GET /api/windows?tool=&set=&state=

## 曝光前替换（双签）

### POST /api/replacements
`{set,mask,new_sha,reason}`（ME/STEWARD）→ `{replacement_id,status:PENDING,requires:[ME,STEWARD]}`。
封存后 → `SET_SEALED_INPLACE`；新构件未登记/隔离/摘要不符/层不适用 → 对应拒收。

### POST /api/replacements/{id}/approve
两次不同自然人、分别覆盖 ME 与 STEWARD。第一签返回 PENDING；
第二签返回 `{status:EXECUTED,new_set_id,new_delivery_no,migrated_windows[]}`，
旧节点 SUPERSEDED、窗口/lot 迁移。同一自然人再签 → `APPROVAL_SAME_PERSON`。

### POST /api/replacements/{id}/reject · GET /api/replacements?status=

## 曝光后 Delta / 返工

### POST /api/deltas（ME）
`{set,changes:[{mask_name,new_sha}],note}`，set 必须已 SEALED（否则 `SET_NOT_SEALED`）。
返回 `{delta_set_id,delivery_no,base_delivery_no,changed_masks}`，base 保持 SEALED。

### POST /api/rework-plans（ME）
`{lot_id,delta,note}`：lot 须已曝光，delta 须与 lot 当前 set 同谱系。

### GET /api/rework-plans

## 只读交付包

### POST /api/packages
`{set}`（幂等；已有 READY 直接复用 `reused:true`；崩溃后自动续建）。
构建时任一构件摘要被篡改 → `DIGEST_TAMPERED`/`PACKAGE_BUILD_FAILED`。

### GET /api/packages · GET /api/packages/{id}
### GET /api/packages/{id}/download
返回确定性未压缩 tar（`MANIFEST.json` + `masks/<mask>/<file>`，成员均 0444），
响应头 `X-Package-Sha256`；下载前重算文件摘要，不符 → `PACKAGE_TAMPERED`。

`MANIFEST.json` 含：`delivery_no,mask_set_id,kind,release_name,die,scribe_um,
corner,fab,foundry_recipe_id,exposure_order,dependencies,masks[],read_only,
manifest_sha256`。

## 实体掩模版匣保管交接链

库房给版匣登记**防拆标签、柜位和责任人**；出库/入库由交出者与接收者**分开确认**。
每笔交接只认一个接收者，请求编号重复送达回放首次结果；同一侧两人争抢确认权仅先完成者取得保管权。
全部状态落 SQLite，服务重启后接续未过期待办并清除失效冻结。

### POST /api/pods（KEEPER 库房管理员）
`{tag_no, cabinet, responsible_person, set?}` →
`{pod_id, tag_no, cabinet, responsible_person, status:IN_STORE}`；
责任人必须具备 `CUSTODIAN` 职责（否则 `HANDOFF_PERSON_NO_DUTY`）；
重复标签 `POD_TAG_EXISTS`。

### GET /api/pods?status= · GET /api/pods/{id 或 tag_no}
版匣详情含当前责任人、冻结申请、开放调查和**历次保管关系** `custody_chain[]`。

### POST /api/handoffs（KEEPER 或 CUSTODIAN）
```json
{"tag_no":"TAG-001","direction":"OUTBOUND","giver":"u_phys1","receiver":"u_phys2",
 "deadline_ts":"2030-01-01T00:00:00Z","cabinet":"A-01","note":""}
```
- 合法：`{status:PENDING, handoff_id, requires:[GIVER,RECEIVER]}`，版匣被该申请**冻结**。
- 下列情况也**留下申请**（`status:REJECTED` + `reject_code/message`，可在
  `GET /api/handoffs?status=REJECTED` 查到），且不占用冻结：
  `HANDOFF_TAG_NOT_FOUND`（标签号码错误，pod_id 为 null）、
  `HANDOFF_PERSON_NO_DUTY`（交出者/接收者无 CUSTODIAN 职责）、
  `HANDOFF_SAME_PERSON`、`HANDOFF_GIVER_NOT_HOLDER`、
  `HANDOFF_FROZEN_BY_OTHER`（已被另一未决申请冻结，detail 给出冻结申请）、
  `HANDOFF_DEADLINE_PASSED`（申请时期限已过）、
  `POD_NOT_IN_STORE` / `POD_NOT_IN_TRANSIT`（方向与版匣状态不符）、
  `POD_UNDER_INVESTIGATION`（调查中禁止出库/再次出库）。
- `deadline_ts` 省略时取服务默认确认期限。

### POST /api/handoffs/{id}/confirm
`{side: GIVER|RECEIVER, note?, inspection?: OK|TAG_DAMAGED}`，
只能由该笔指定的交出者/接收者本人提交（否则 403 `HANDOFF_NOT_DESIGNATED`）。
第一签返回 `status:PENDING`；两签齐备移交保管权（出库→`IN_TRANSIT`，入库→`IN_STORE`，
入库时 `cabinet` 归位）。同一侧并发争抢：主键兜底，先完成者成功，余者
`HANDOFF_SIDE_TAKEN`；已有一方确认后撤销/换人分别被
`HANDOFF_CONFIRMATION_EXISTS` / `HANDOFF_PARTY_LOCKED` 拒绝。
确认时已过期限 → `HANDOFF_DEADLINE_PASSED`，申请置 `EXPIRED` 并清除版匣冻结；
对 `REJECTED/CONFIRMED/CANCELLED/EXPIRED` 申请的晚到确认一律 `HANDOFF_DECIDED`/原拒绝码，不推翻既有决定。
入库接收确认携带 `inspection:"TAG_DAMAGED"` 时，保管权归位后**立即转入调查**并禁止再次出库。

### POST /api/handoffs/{id}/cancel · POST /api/handoffs/{id}/amend
撤销仅允许**申请人或 KEEPER**，已有一方确认即禁止；amend 替换交出者/接收者
`{giver,receiver,deadline_ts?}`，规则同上，首签后 `HANDOFF_PARTY_LOCKED`。

### POST /api/pods/{id}/inspect（KEEPER）
`{result: OK|TAG_DAMAGED, note}`：标签破损 → 自动开启调查（版匣
`UNDER_INVESTIGATION`，禁止出库与发生产线）；`GET /api/inspections?pod=` 可查。

### POST /api/pods/{id}/release-line（KEEPER）
**双方确认结束之前不得把版匣发给生产线**：无已确认出库交接或仍有未决冻结 →
`POD_HANDOFF_NOT_CONFIRMED`；调查中 → `POD_UNDER_INVESTIGATION`。
成功记录 `line_released_at`，重复调用幂等 `noop:true`。

### 调查：POST /api/investigations · POST /api/investigations/{id}/confirm · GET /api/investigations?status=
安全员/库房可直接登记调查 `{pod,reason,evidence?}`。解除调查必须由
**安全员 SAFETY 与工艺负责人 PROCESS 两个不同自然人**分别确认
`{role,new_holder}`：第一签返回 `requires`；同一自然人即使兼两职也以
`INVESTIGATION_SAME_PERSON` 拒绝；双签齐备 → 调查 `RELEASED`、版匣回 `IN_STORE`、
责任人改为 `new_holder`（须有 CUSTODIAN 职责）。**先前保管关系仍可查询**。

### 查询：GET /api/todos?person= · GET /api/custody/{id或tag} · GET /api/handoffs?status=&pod=&person= · GET /api/custody-exceptions
待办返回该自然人尚未确认的一侧 `waiting_for_me[]`；保管关系给出完整保管链；
异常详情汇总被拒/过期交接、标签破损检查与全部调查。

## 隔离处置 / 审计

### GET /api/quarantine?status=
同名构件/同名发行隔离项。

### POST /api/quarantine/{id}/resolve（ADMIN）
`{decision: ACCEPT_AS_ALIAS|REJECT, new_name?}`；接受同名发行后自动补建 mask set
并把发行置 VALID。

### GET /api/arbitrations
资源裁决记录（resource_key、区间、winner_op_id、loser_op_id、decided_at）。

### GET /api/audit?limit=&target=&op=
全部操作日志（资料、占用、批准、谱系、打包、恢复）。

### GET /api/people · POST /api/people（ADMIN）
自然人列表 / 新建 `{id,name,roles[]}`（ME/STEWARD/DISPATCHER/ADMIN）。
