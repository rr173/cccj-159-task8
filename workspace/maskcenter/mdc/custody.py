"""实体掩模版匣保管交接链。

所有裁决都在 SQLite 的 BEGIN IMMEDIATE 事务中完成：
- 请求、双方确认、撤销、入库检查、调查和解除调查均落盘；
- 每个交接只有一个接收者，未指定时由第一个有效接收确认锁定；
- 重启后接续未过期待办，并将过期冻结归还原责任人。
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .domain import DomainError
from .store import utcnow

if TYPE_CHECKING:
    from .service import Service

# 保管请求标识需要在确认期限结束后仍可重放并返回第一次裁决，不能使用普通短 TTL。
CUSTODY_OP_TTL_SECONDS = 3600 * 24 * 365 * 100
WAREHOUSE_ROLES = ("WAREHOUSE", "WAREHOUSE_ADMIN")
OUT_ROLE = {"surrender": "WAREHOUSE", "receiver": "LINE"}
IN_ROLE = {"surrender": "LINE", "receiver": "WAREHOUSE"}


def _parse_ts(value: str):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class CustodyService:
    def __init__(self, service: "Service"):
        self.svc = service
        self.s = service.s

    # ------------------------------------------------------------ 基础校验
    @staticmethod
    def _person(row) -> dict:
        return {"id": row["id"], "name": row["name"],
                "roles": json.loads(row["roles"])}

    def _known_person(self, person_id: str) -> dict:
        if not person_id:
            raise DomainError("UNAUTHORIZED", "缺少 X-Person-Id 操作者身份", 401)
        row = self.s.q_one("SELECT * FROM person WHERE id=?", (person_id,))
        if not row:
            raise DomainError("UNAUTHORIZED", f"未知自然人：{person_id}", 401)
        return self._person(row)

    def _load_person(self, person_id: str, label: str = "人员") -> dict:
        row = self.s.q_one("SELECT * FROM person WHERE id=?", (person_id,))
        if not row:
            raise DomainError("PERSON_NOT_FOUND", f"{label}不存在：{person_id}", 404,
                              {"person": person_id})
        return self._person(row)

    @staticmethod
    def _has_role(person: dict, role: str) -> bool:
        return role in person["roles"]

    def _pod_dict(self, row) -> dict[str, Any]:
        d = dict(row)
        d["current_custodian"] = {"person_id": row["owner_person_id"]}
        p = self.s.q_one("SELECT name,roles FROM person WHERE id=?",
                         (row["owner_person_id"],))
        d["current_custodian"]["name"] = p["name"] if p else None
        if p:
            d["current_custodian"]["roles"] = json.loads(p["roles"])
        d["active_transfer"] = None
        active = self.s.q_one(
            "SELECT * FROM custody_transfer WHERE pod_id=? AND status='PENDING'",
            (row["id"],))
        if active:
            d["active_transfer"] = self.transfer_dict(active)
        d["open_investigation"] = None
        inv = self.s.q_one(
            "SELECT * FROM custody_investigation WHERE pod_id=? AND status='OPEN'"
            " ORDER BY opened_at DESC LIMIT 1", (row["id"],))
        if inv:
            d["open_investigation"] = self.investigation_dict(inv)
        return d

    def _load_pod(self, pod_key: str):
        row = self.s.q_one(
            "SELECT * FROM custody_pod WHERE id=? OR label_no=?",
            (pod_key, pod_key))
        if not row:
            raise DomainError("POD_NOT_FOUND", f"实体版匣不存在：{pod_key}", 404,
                              {"pod": pod_key})
        return row

    def _relation(self, c, pod_id, event, *, transfer_id=None, from_person=None,
                  to_person=None, person_id=None, cabinet=None, label_no=None,
                  detail=None):
        c.execute(
            "INSERT INTO custody_relation(pod_id,transfer_id,event,from_person,"
            "to_person,person_id,cabinet,label_no,at,detail_json)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (pod_id, transfer_id, event, from_person, to_person, person_id,
             cabinet, label_no, utcnow(),
             json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)))

    def _rejected_transfer(self, actor: dict, pod_id: str | None, direction: str,
                           surrender: str | None, receiver: str | None,
                           label_no: str, cabinet: str, note: str,
                           deadline_at: str, reason: str, op_id: str | None,
                           audit_action="custody.transfer.reject"):
        tid = "CTR-" + secrets.token_hex(6)
        now = utcnow()
        with self.s.tx() as c:
            c.execute(
                "INSERT INTO custody_transfer(id,pod_id,direction,surrender_person,"
                "receiver_person,requested_by,status,reject_reason,cabinet,note,"
                "deadline_at,created_at,decided_at) VALUES(?,?,?,?,?,?,'REJECTED',"
                "?,?,?,?,?,?)",
                (tid, pod_id, direction or "UNKNOWN", surrender or "", receiver or "", actor["id"],
                 reason, cabinet, note, deadline_at, now, now))
            self.s.audit(c, actor["id"], audit_action, tid, op_id=op_id,
                         detail={"reason": reason, "pod": pod_id, "label": label_no})
        return tid

    def _reject_persist(self, code: str, message: str, *, actor: dict,
                        pod_id: str | None, direction: str, surrender: str | None,
                        receiver: str | None, label_no: str, cabinet: str,
                        note: str, deadline_at: str, op_id: str | None,
                        http_status: int = 409) -> DomainError:
        tid = self._rejected_transfer(
            actor, pod_id, direction, surrender, receiver, label_no, cabinet, note,
            deadline_at, code, op_id)
        return DomainError(code, message, http_status,
                           {"transfer_id": tid, "reason": code, "pod": pod_id,
                            "label_no": label_no})

    # ------------------------------------------------------------ 登记版匣
    def register_pod(self, actor_id: str, set_key: str, label_no: str,
                     cabinet: str, owner_person_id: str | None = None,
                     op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        payload = {"set": set_key, "label_no": label_no, "cabinet": cabinet,
                   "owner_person_id": owner_person_id}

        def action():
            if not any(r in actor["roles"] for r in WAREHOUSE_ROLES):
                raise DomainError("UNAUTHORIZED", "只有库房人员可登记实体版匣", 403)
            label = (label_no or "").strip()
            loc = (cabinet or "").strip()
            if not label:
                raise DomainError("LABEL_REQUIRED", "防拆标签号码必填", 400)
            if not loc:
                raise DomainError("CABINET_REQUIRED", "柜位必填", 400)
            s = self.svc._load_set(set_key)
            if self.s.q_one("SELECT 1 FROM custody_pod WHERE label_no=?", (label,)):
                raise DomainError("LABEL_ALREADY_EXISTS",
                                  f"防拆标签号码已登记：{label}", 409)
            if self.s.q_one("SELECT 1 FROM custody_pod WHERE set_id=?", (s["id"],)):
                raise DomainError("POD_ALREADY_REGISTERED",
                                  "该 mask set 已有实体版匣登记", 409)
            owner_id = owner_person_id or actor["id"]
            owner = self._load_person(owner_id, "责任人")
            if not any(r in owner["roles"] for r in WAREHOUSE_ROLES):
                raise DomainError("UNAUTHORIZED",
                                  "库房版匣责任人必须具备 WAREHOUSE 职责", 403,
                                  {"person": owner_id})
            pid = "CTP-" + secrets.token_hex(6)
            now = utcnow()
            with self.s.tx() as c:
                c.execute(
                    "INSERT INTO custody_pod(id,set_id,label_no,cabinet,"
                    "owner_person_id,status,created_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?, 'IN_STORAGE',?,?,?)",
                    (pid, s["id"], label, loc, owner_id, actor["id"], now, now))
                self._relation(c, pid, "REGISTERED", to_person=owner_id,
                               person_id=actor["id"], cabinet=loc, label_no=label,
                               detail={"set_id": s["id"], "delivery_no": s["delivery_no"]})
                self.s.audit(c, actor["id"], "custody.pod.register", pid,
                             op_id=op_id, detail={"label": label, "cabinet": loc,
                                                  "set": s["delivery_no"]})
            return {"pod_id": pid, "set_id": s["id"], "delivery_no": s["delivery_no"],
                    "label_no": label, "cabinet": loc,
                    "owner_person_id": owner_id, "status": "IN_STORAGE"}
        return self.svc.idempotent(op_id, "custody.register", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    # ------------------------------------------------------------ 交接申请
    def request_transfer(self, actor_id: str, pod_key: str, direction: str,
                         label_no: str, receiver_person_id: str | None = None,
                         surrender_person_id: str | None = None,
                         cabinet: str | None = None, note: str = "",
                         ttl_seconds: int | None = None,
                         deadline_at: str | None = None,
                         op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        direction = (direction or "").upper()
        payload = {"pod": pod_key, "direction": direction, "label_no": label_no,
                   "receiver": receiver_person_id,
                   "surrender": surrender_person_id, "cabinet": cabinet,
                   "note": note, "deadline_at": deadline_at,
                   "ttl_seconds": ttl_seconds}
        ttl = int(ttl_seconds or self.svc.ttl)
        if not 30 <= ttl <= 24 * 3600:
            # 不需要留交接申请：请求本身的时间窗参数非法。
            raise DomainError("BAD_TTL", "确认期限必须在 30 秒到 86400 秒之间", 400)
        now = _parse_ts(utcnow())
        deadline = (_parse_ts(deadline_at) if deadline_at
                    else now + timedelta(seconds=ttl))
        deadline_text = deadline.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        def action():
            if direction not in ("OUTBOUND", "INBOUND"):
                raise DomainError("BAD_DIRECTION",
                                  "direction 必须是 OUTBOUND 或 INBOUND", 400)
            pod = self.s.q_one(
                "SELECT * FROM custody_pod WHERE id=? OR label_no=?",
                (pod_key, pod_key))
            pod_id = pod["id"] if pod else None
            target_cabinet = (cabinet or (pod["cabinet"] if pod else "") or "").strip()
            surrender_id = surrender_person_id or (pod["owner_person_id"] if pod else "")
            reject_kw = dict(actor=actor, pod_id=pod_id, direction=direction,
                             surrender=surrender_id, receiver=receiver_person_id,
                             label_no=label_no or (pod["label_no"] if pod else ""),
                             cabinet=target_cabinet, note=note,
                             deadline_at=deadline_text, op_id=op_id)

            if not pod:
                raise self._reject_persist(
                    "POD_NOT_FOUND", f"实体版匣不存在：{pod_key}", **reject_kw,
                    http_status=404)
            if not label_no or label_no != pod["label_no"]:
                raise self._reject_persist(
                    "LABEL_MISMATCH",
                    "扫码标签号码与版匣登记的防拆标签不一致", **reject_kw,
                    http_status=400)
            if deadline <= now:
                raise self._reject_persist(
                    "CONFIRMATION_DEADLINE_PASSED",
                    "交接确认期限已过，不能创建新申请", **reject_kw)
            active = self.s.q_one(
                "SELECT * FROM custody_transfer WHERE pod_id=? AND status='PENDING'",
                (pod["id"],))
            if active:
                raise self._reject_persist(
                    "POD_FROZEN",
                    f"版匣已被交接申请 {active['id']} 冻结", **reject_kw,
                    http_status=409)
            investigation = self.s.q_one(
                "SELECT * FROM custody_investigation WHERE pod_id=? AND status='OPEN'",
                (pod["id"],))
            if investigation:
                raise self._reject_persist(
                    "POD_UNDER_INVESTIGATION",
                    f"版匣处于调查 {investigation['id']}，禁止再次出库", **reject_kw)

            surrender_row = self.s.q_one("SELECT * FROM person WHERE id=?",
                                         (surrender_id,)) if surrender_id else None
            surrender = self._person(surrender_row) if surrender_row else None
            roles = OUT_ROLE if direction == "OUTBOUND" else IN_ROLE
            expected_status = "IN_STORAGE" if direction == "OUTBOUND" else "ISSUED_TO_LINE"
            if receiver_person_id:
                receiver_row = self.s.q_one(
                    "SELECT * FROM person WHERE id=?", (receiver_person_id,))
                if not receiver_row:
                    raise self._reject_persist(
                        "PERSON_ROLE_INVALID",
                        f"接收者不存在或缺少 {roles['receiver']} 职责", **reject_kw,
                        http_status=404)
                receiver = self._person(receiver_row)
            else:
                receiver = None
            if pod["status"] != expected_status:
                raise self._reject_persist(
                    "POD_INVALID_STATE",
                    f"{direction} 要求版匣处于 {expected_status}，当前 {pod['status']}",
                    **reject_kw)
            if not surrender:
                raise self._reject_persist(
                    "PERSON_ROLE_INVALID",
                    f"交出者不存在或缺少 {roles['surrender']} 职责", **reject_kw,
                    http_status=404)
            if pod["owner_person_id"] != surrender["id"]:
                raise self._reject_persist(
                    "SURRENDER_NOT_CUSTODIAN",
                    "交出者不是当前保管责任人，不能移交该版匣", **reject_kw)
            if actor["id"] != surrender["id"] and "WAREHOUSE_ADMIN" not in actor["roles"]:
                raise self._reject_persist(
                    "UNAUTHORIZED",
                    "只有当前保管责任人或库房管理员可发起交接", **reject_kw,
                    http_status=403)
            if not self._has_role(surrender, roles["surrender"]):
                raise self._reject_persist(
                    "PERSON_ROLE_INVALID",
                    f"交出者缺少 {roles['surrender']} 职责", **reject_kw, http_status=403)
            if receiver is not None and not self._has_role(receiver, roles["receiver"]):
                raise self._reject_persist(
                    "PERSON_ROLE_INVALID",
                    f"接收者缺少 {roles['receiver']} 职责", **reject_kw, http_status=403)
            if receiver is not None and surrender["id"] == receiver["id"]:
                raise self._reject_persist(
                    "TRANSFER_SAME_PERSON",
                    "交出者与接收者必须是不同自然人", **reject_kw)

            tid = "CTR-" + secrets.token_hex(6)
            pending_status = ("OUTBOUND_PENDING" if direction == "OUTBOUND"
                              else "INBOUND_PENDING")
            with self.s.tx() as c:
                c.execute(
                    "INSERT INTO custody_transfer(id,pod_id,direction,"
                    "surrender_person,receiver_person,requested_by,status,cabinet,"
                    "note,deadline_at,created_at) VALUES(?,?,?,?,?,?,'PENDING',?,?,?,?)",
                    (tid, pod["id"], direction, surrender["id"],
                     receiver["id"] if receiver else None,
                     actor["id"], target_cabinet, note, deadline_text, utcnow()))
                c.execute("UPDATE custody_pod SET status=?,updated_at=? WHERE id=?",
                          (pending_status, utcnow(), pod["id"]))
                self.s.audit(c, actor["id"], "custody.transfer.request", tid,
                             op_id=op_id,
                             detail={"pod": pod["id"], "label": pod["label_no"],
                                     "direction": direction,
                                     "surrender": surrender["id"],
                                     "receiver": receiver["id"] if receiver else None,
                                     "deadline_at": deadline_text})
            return {"transfer_id": tid, "pod_id": pod["id"], "direction": direction,
                    "status": "PENDING", "surrender_person": surrender["id"],
                    "receiver_person": receiver["id"] if receiver else None, "deadline_at": deadline_text,
                    "confirmations": []}
        return self.svc.idempotent(op_id, "custody.transfer.request", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    def transfer_dict(self, row) -> dict[str, Any]:
        d = dict(row)
        d["confirmations"] = [dict(r) for r in self.s.q_all(
            "SELECT side,person_id,role,at,op_id FROM custody_confirmation"
            " WHERE transfer_id=? ORDER BY at", (row["id"],))]
        return d

    def _expire_if_due(self, c, t) -> bool:
        if t["status"] != "PENDING" or t["deadline_at"] >= utcnow():
            return False
        restore_status = ("IN_STORAGE" if t["direction"] == "OUTBOUND"
                          else "ISSUED_TO_LINE")
        c.execute("UPDATE custody_transfer SET status='EXPIRED',decided_at=? WHERE id=?",
                  (utcnow(), t["id"]))
        c.execute("UPDATE custody_pod SET status=?,owner_person_id=?,updated_at=?"
                  " WHERE id=?",
                  (restore_status, t["surrender_person"], utcnow(), t["pod_id"]))
        self._relation(c, t["pod_id"], "TRANSFER_EXPIRED", transfer_id=t["id"],
                       from_person=t["surrender_person"], to_person=t["surrender_person"],
                       detail={"restore_status": restore_status})
        self.s.audit(c, "system", "custody.transfer.expire", t["id"],
                     detail={"pod": t["pod_id"]})
        return True

    def _expire_due_transfer(self, transfer_id: str):
        """在新事务中先固化到期裁决，避免随后的确认失败回滚解冻。"""
        with self.s.tx() as c:
            t = c.execute("SELECT * FROM custody_transfer WHERE id=?",
                          (transfer_id,)).fetchone()
            if t:
                self._expire_if_due(c, t)

    def confirm_transfer(self, actor_id: str, transfer_id: str,
                         op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        payload = {"transfer_id": transfer_id, "by": actor["id"]}

        def action():
            self._expire_due_transfer(transfer_id)
            with self.s.tx() as c:
                t = c.execute("SELECT * FROM custody_transfer WHERE id=?",
                              (transfer_id,)).fetchone()
                if not t:
                    raise DomainError("TRANSFER_NOT_FOUND",
                                      f"交接申请不存在：{transfer_id}", 404)
                if t["status"] != "PENDING":
                    if t["status"] == "EXPIRED":
                        raise DomainError("CONFIRMATION_DEADLINE_PASSED",
                                          "确认期限已过，迟到确认不能改变决定", 409,
                                          {"transfer_id": transfer_id})
                    raise DomainError("TRANSFER_DECIDED",
                                      f"交接申请已 {t['status']}，迟到确认不能改变决定",
                                      409, {"status": t["status"],
                                            "reason": t["reject_reason"]})
                if self._expire_if_due(c, t):
                    raise DomainError("CONFIRMATION_DEADLINE_PASSED",
                                      "确认期限已过，冻结已清除", 409,
                                      {"transfer_id": transfer_id})
                roles = OUT_ROLE if t["direction"] == "OUTBOUND" else IN_ROLE
                prior = c.execute(
                    "SELECT * FROM custody_confirmation WHERE transfer_id=?",
                    (transfer_id,)).fetchall()
                if actor["id"] == t["surrender_person"]:
                    side, required_role = "SURRENDER", roles["surrender"]
                elif t["receiver_person"] and actor["id"] == t["receiver_person"]:
                    side, required_role = "RECEIVER", roles["receiver"]
                elif not t["receiver_person"] and self._has_role(
                        actor, roles["receiver"]):
                    # 不预选接收者时，第一个有效接收确认者成为唯一接收者。
                    side, required_role = "RECEIVER", roles["receiver"]
                else:
                    raise DomainError("RECEIVER_MISMATCH",
                                      "操作者不是该笔交接指定的交出者或接收者", 403,
                                      {"transfer_id": transfer_id})
                if not self._has_role(actor, required_role):
                    raise DomainError("PERSON_ROLE_INVALID",
                                      f"该确认方需要 {required_role} 职责", 403,
                                      {"person": actor["id"], "required_role": required_role})
                if any(r["person_id"] == actor["id"] and r["side"] != side for r in prior):
                    raise DomainError("CONFIRMATION_SAME_PERSON",
                                      "同一自然人不能分别代表交接双方确认", 409)
                existing = c.execute(
                    "SELECT 1 FROM custody_confirmation WHERE transfer_id=? AND side=?",
                    (transfer_id, side)).fetchone()
                if existing:
                    return {"transfer_id": transfer_id, "status": "PENDING",
                            "already_confirmed": True,
                            "confirmations": [dict(r) for r in prior]}

                claimed_receiver = False
                if side == "RECEIVER" and not t["receiver_person"]:
                    # 条件更新由 BEGIN IMMEDIATE 串行化：并发接收者只有一个能把人锁定。
                    cur = c.execute(
                        "UPDATE custody_transfer SET receiver_person=?"
                        " WHERE id=? AND receiver_person IS NULL",
                        (actor["id"], transfer_id))
                    if cur.rowcount != 1:
                        raise DomainError("RECEIVER_MISMATCH",
                                          "已有其他接收者先完成确认并取得保管权", 409,
                                          {"transfer_id": transfer_id})
                    claimed_receiver = True

                c.execute(
                    "INSERT INTO custody_confirmation(transfer_id,side,person_id,"
                    "role,at,op_id) VALUES(?,?,?,?,?,?)",
                    (transfer_id, side, actor["id"], required_role, utcnow(), op_id))
                self.s.audit(c, actor["id"], "custody.transfer.confirm", transfer_id,
                             op_id=op_id, detail={"side": side, "role": required_role})
                confirmations = [dict(r) for r in c.execute(
                    "SELECT side,person_id,role,at FROM custody_confirmation"
                    " WHERE transfer_id=? ORDER BY at", (transfer_id,)).fetchall()]
                if len(confirmations) < 2:
                    locked = (t["receiver_person"] or
                              (actor["id"] if side == "RECEIVER" else None))
                    if claimed_receiver:
                        locked = actor["id"]
                    return {"transfer_id": transfer_id, "status": "PENDING",
                            "confirmations": confirmations,
                            "locked_receiver": locked}

                now = utcnow()
                receiver_id = t["receiver_person"] or (
                    actor["id"] if side == "RECEIVER" else None)
                if claimed_receiver:
                    receiver_id = actor["id"]
                if t["direction"] == "OUTBOUND":
                    new_status, new_owner = "ISSUED_TO_LINE", receiver_id
                    event = "OUTBOUND_COMPLETED"
                    cabinet = ""
                else:
                    new_status, new_owner = "AWAITING_INSPECTION", receiver_id
                    event = "INBOUND_COMPLETED"
                    cabinet = t["cabinet"]
                c.execute(
                    "UPDATE custody_transfer SET status='COMPLETED',completed_at=?,"
                    "decided_at=? WHERE id=?", (now, now, transfer_id))
                c.execute(
                    "UPDATE custody_pod SET status=?,owner_person_id=?,cabinet=?,"
                    "updated_at=? WHERE id=?",
                    (new_status, new_owner, cabinet, now, t["pod_id"]))
                self._relation(c, t["pod_id"], event, transfer_id=transfer_id,
                               from_person=t["surrender_person"],
                               to_person=receiver_id, person_id=actor["id"],
                               cabinet=cabinet,
                               detail={"direction": t["direction"],
                                       "confirmations": confirmations})
                self.s.audit(c, actor["id"], "custody.transfer.complete", transfer_id,
                             op_id=op_id, detail={"new_status": new_status,
                                                  "owner": new_owner})
                return {"transfer_id": transfer_id, "status": "COMPLETED",
                        "pod_id": t["pod_id"], "new_pod_status": new_status,
                        "owner_person_id": new_owner,
                        "issued_to_line": new_status == "ISSUED_TO_LINE",
                        "confirmations": confirmations}
        return self.svc.idempotent(op_id, "custody.transfer.confirm", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    # ------------------------------------------------------------ 撤销
    def cancel_transfer(self, actor_id: str, transfer_id: str,
                        op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        payload = {"transfer_id": transfer_id, "by": actor["id"]}

        def action():
            self._expire_due_transfer(transfer_id)
            with self.s.tx() as c:
                t = c.execute("SELECT * FROM custody_transfer WHERE id=?",
                              (transfer_id,)).fetchone()
                if not t:
                    raise DomainError("TRANSFER_NOT_FOUND", "交接申请不存在", 404)
                if t["status"] in ("CANCELLED",):
                    return {"transfer_id": transfer_id, "status": "CANCELLED",
                            "noop": True}
                if t["status"] != "PENDING":
                    raise DomainError("TRANSFER_DECIDED",
                                      f"交接已 {t['status']}，再次撤销不能推翻决定",
                                      409, {"status": t["status"]})
                if self._expire_if_due(c, t):
                    raise DomainError("CONFIRMATION_DEADLINE_PASSED",
                                      "确认期限已过，冻结已自动清除", 409)
                if actor["id"] != t["requested_by"] and "WAREHOUSE_ADMIN" not in actor["roles"]:
                    raise DomainError("UNAUTHORIZED",
                                      "只有申请人或库房管理员可撤销交接", 403)
                restore_status = ("IN_STORAGE" if t["direction"] == "OUTBOUND"
                                  else "ISSUED_TO_LINE")
                now = utcnow()
                c.execute("UPDATE custody_transfer SET status='CANCELLED',cancelled_at=?,"
                          "decided_at=? WHERE id=?", (now, now, transfer_id))
                c.execute("UPDATE custody_pod SET status=?,owner_person_id=?,updated_at=?"
                          " WHERE id=?",
                          (restore_status, t["surrender_person"], now, t["pod_id"]))
                self._relation(c, t["pod_id"], "TRANSFER_CANCELLED",
                               transfer_id=transfer_id, person_id=actor["id"],
                               from_person=t["surrender_person"],
                               to_person=t["surrender_person"],
                               detail={"restore_status": restore_status})
                self.s.audit(c, actor["id"], "custody.transfer.cancel", transfer_id,
                             op_id=op_id, detail={"restore_status": restore_status})
                return {"transfer_id": transfer_id, "status": "CANCELLED",
                        "pod_status": restore_status}
        return self.svc.idempotent(op_id, "custody.transfer.cancel", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    # ------------------------------------------------------------ 入库检查/调查
    def inspect_inbound(self, actor_id: str, pod_key: str, result: str,
                        note: str = "", op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        result = (result or "").upper()
        payload = {"pod": pod_key, "result": result, "note": note, "by": actor["id"]}

        def action():
            if not any(r in actor["roles"] for r in WAREHOUSE_ROLES):
                raise DomainError("UNAUTHORIZED", "只有库房人员可做入库检查", 403)
            if result not in ("INTACT", "LABEL_DAMAGED"):
                raise DomainError("BAD_INSPECTION_RESULT",
                                  "result 必须是 INTACT 或 LABEL_DAMAGED", 400)
            pod = self._load_pod(pod_key)
            transfer = self.s.q_one(
                "SELECT * FROM custody_transfer WHERE pod_id=? AND status='COMPLETED'"
                " AND direction='INBOUND' ORDER BY completed_at DESC LIMIT 1",
                (pod["id"],))
            existing = self.s.q_one(
                "SELECT * FROM custody_inspection WHERE pod_id=? AND result=?"
                " ORDER BY at DESC LIMIT 1", (pod["id"], result))
            if existing:
                return {"inspection_id": existing["id"], "result": existing["result"],
                        "noop": True}
            if pod["status"] != "AWAITING_INSPECTION":
                raise DomainError("POD_NOT_AWAITING_INSPECTION",
                                  f"版匣当前 {pod['status']}，不能执行入库检查", 409)
            iid = "INS-" + secrets.token_hex(6)
            now = utcnow()
            with self.s.tx() as c:
                c.execute(
                    "INSERT INTO custody_inspection(id,pod_id,transfer_id,result,"
                    "inspector_person,label_no,at,note) VALUES(?,?,?,?,?,?,?,?)",
                    (iid, pod["id"], transfer["id"] if transfer else None, result,
                     actor["id"], pod["label_no"], now, note))
                if result == "INTACT":
                    c.execute("UPDATE custody_pod SET status='IN_STORAGE',updated_at=?"
                              " WHERE id=?", (now, pod["id"]))
                    self._relation(c, pod["id"], "INSPECTION_INTACT",
                                   transfer_id=transfer["id"] if transfer else None,
                                   person_id=actor["id"], cabinet=pod["cabinet"],
                                   label_no=pod["label_no"], detail={"note": note})
                    self.s.audit(c, actor["id"], "custody.inspection.intact", iid,
                                 op_id=op_id, detail={"pod": pod["id"]})
                    out_status = "IN_STORAGE"
                    investigation_id = None
                else:
                    inv_id = "INV-" + secrets.token_hex(6)
                    c.execute("UPDATE custody_pod SET status='INVESTIGATING',updated_at=?"
                              " WHERE id=?", (now, pod["id"]))
                    c.execute(
                        "INSERT INTO custody_investigation(id,pod_id,inspection_id,"
                        "status,reason,opened_by,opened_at) VALUES(?,?,?, 'OPEN',?,?,?)",
                        (inv_id, pod["id"], iid, "LABEL_DAMAGED", actor["id"], now))
                    self._relation(c, pod["id"], "INVESTIGATION_OPENED",
                                   transfer_id=transfer["id"] if transfer else None,
                                   person_id=actor["id"], label_no=pod["label_no"],
                                   detail={"inspection": iid, "reason": "LABEL_DAMAGED"})
                    self.s.audit(c, actor["id"], "custody.investigation.open", inv_id,
                                 op_id=op_id,
                                 detail={"pod": pod["id"], "inspection": iid})
                    out_status = "INVESTIGATING"
                    investigation_id = inv_id
            return {"inspection_id": iid, "pod_id": pod["id"], "result": result,
                    "pod_status": out_status, "investigation_id": investigation_id}
        return self.svc.idempotent(op_id, "custody.inspection", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    def relabel_during_investigation(self, actor_id: str, pod_key: str,
                                     new_label_no: str, op_id: str | None = None):
        actor = self._known_person(actor_id)
        payload = {"pod": pod_key, "new_label": new_label_no, "by": actor["id"]}

        def action():
            if "WAREHOUSE_ADMIN" not in actor["roles"]:
                raise DomainError("UNAUTHORIZED",
                                  "只有库房管理员可在调查中更换防拆标签", 403)
            label = (new_label_no or "").strip()
            if not label:
                raise DomainError("LABEL_REQUIRED", "新防拆标签号码必填", 400)
            pod = self._load_pod(pod_key)
            inv = self.s.q_one(
                "SELECT * FROM custody_investigation WHERE pod_id=? AND status='OPEN'"
                " ORDER BY opened_at DESC LIMIT 1", (pod["id"],))
            if not inv:
                raise DomainError("INVESTIGATION_NOT_OPEN", "版匣没有开放中的调查", 409)
            if self.s.q_one("SELECT 1 FROM custody_pod WHERE label_no=? AND id!=?",
                            (label, pod["id"])):
                raise DomainError("LABEL_ALREADY_EXISTS", "新标签号码已被占用", 409)
            old = pod["label_no"]
            with self.s.tx() as c:
                c.execute("UPDATE custody_pod SET label_no=?,updated_at=? WHERE id=?",
                          (label, utcnow(), pod["id"]))
                c.execute("UPDATE custody_investigation SET new_label_no=? WHERE id=?",
                          (label, inv["id"]))
                self._relation(c, pod["id"], "LABEL_REPLACED", person_id=actor["id"],
                               label_no=label, detail={"old_label_no": old})
                self.s.audit(c, actor["id"], "custody.label.replace", pod["id"],
                             op_id=op_id, detail={"old": old, "new": label})
            return {"pod_id": pod["id"], "investigation_id": inv["id"],
                    "old_label_no": old, "label_no": label}
        return self.svc.idempotent(op_id, "custody.relabel", payload, action,
                                   actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    def investigation_dict(self, row) -> dict[str, Any]:
        d = dict(row)
        d["confirmations"] = [dict(r) for r in self.s.q_all(
            "SELECT role,person_id,at,op_id FROM investigation_confirmation"
            " WHERE investigation_id=? ORDER BY at", (row["id"],))]
        return d

    def confirm_investigation(self, actor_id: str, investigation_id: str,
                              op_id: str | None = None) -> dict:
        actor = self._known_person(actor_id)
        payload = {"investigation_id": investigation_id, "by": actor["id"]}

        def action():
            with self.s.tx() as c:
                inv = c.execute("SELECT * FROM custody_investigation WHERE id=?",
                                (investigation_id,)).fetchone()
                if not inv:
                    raise DomainError("INVESTIGATION_NOT_FOUND", "调查不存在", 404)
                if inv["status"] != "OPEN":
                    return {"investigation_id": investigation_id,
                            "status": inv["status"], "noop": True}
                prior = c.execute(
                    "SELECT * FROM investigation_confirmation WHERE investigation_id=?",
                    (investigation_id,)).fetchall()
                done_roles = {r["role"] for r in prior}
                roles_held = [r for r in ("SAFETY", "PROCESS")
                              if self._has_role(actor, r)]
                if not roles_held:
                    raise DomainError("UNAUTHORIZED",
                                      "解除调查只能由安全员或工艺负责人确认", 403)
                missing_roles = [r for r in roles_held if r not in done_roles]
                if not missing_roles:
                    raise DomainError("INVESTIGATION_SAME_PERSON",
                                      "安全员与工艺负责人必须是不同自然人", 409)
                role = missing_roles[0]
                if any(r["person_id"] == actor["id"] for r in prior):
                    raise DomainError("INVESTIGATION_SAME_PERSON",
                                      "安全员与工艺负责人必须是不同自然人", 409)
                if not missing_roles:
                    raise DomainError("INVESTIGATION_SAME_PERSON",
                                      "安全员与工艺负责人必须是不同自然人", 409)
                role = missing_roles[0]
                if len({r["person_id"] for r in prior} | {actor["id"]}) < len(prior) + 1:
                    raise DomainError("INVESTIGATION_SAME_PERSON",
                                      "安全员与工艺负责人必须是不同自然人", 409)
                if not inv["new_label_no"]:
                    raise DomainError("LABEL_REPLACEMENT_REQUIRED",
                                      "破损标签必须先由库房管理员更换，才能收集确认",
                                      409, {"investigation_id": investigation_id})
                c.execute(
                    "INSERT INTO investigation_confirmation(investigation_id,role,"
                    "person_id,at,op_id) VALUES(?,?,?,?,?)",
                    (investigation_id, role, actor["id"], utcnow(), op_id))
                self.s.audit(c, actor["id"], "custody.investigation.confirm",
                             investigation_id, op_id=op_id, detail={"role": role})
                confirms = c.execute(
                    "SELECT * FROM investigation_confirmation WHERE investigation_id=?",
                    (investigation_id,)).fetchall()
                if len(confirms) < 2:
                    return {"investigation_id": investigation_id, "status": "OPEN",
                            "confirmations": [dict(r) for r in confirms]}
                now = utcnow()
                c.execute("UPDATE custody_investigation SET status='RELEASED',closed_at=?"
                          " WHERE id=?", (now, investigation_id))
                c.execute("UPDATE custody_pod SET status='IN_STORAGE',updated_at=?"
                          " WHERE id=?", (now, inv["pod_id"]))
                self._relation(c, inv["pod_id"], "INVESTIGATION_RELEASED",
                               person_id=actor["id"], label_no=inv["new_label_no"],
                               detail={"confirmations": [r["person_id"] for r in confirms]})
                self.s.audit(c, actor["id"], "custody.investigation.release",
                             investigation_id, op_id=op_id,
                             detail={"pod": inv["pod_id"]})
                return {"investigation_id": investigation_id, "status": "RELEASED",
                        "pod_id": inv["pod_id"], "pod_status": "IN_STORAGE",
                        "label_no": inv["new_label_no"]}
        return self.svc.idempotent(op_id, "custody.investigation.confirm", payload,
                                   action, actor=actor["id"],
                                   ttl_seconds=CUSTODY_OP_TTL_SECONDS)

    # ------------------------------------------------------------ 查询
    def list_pods(self, status: str | None = None):
        sql = "SELECT * FROM custody_pod"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at"
        return [self._pod_dict(r) for r in self.s.q_all(sql, args)]

    def get_pod(self, key: str) -> dict[str, Any]:
        return self._pod_dict(self._load_pod(key))

    def list_transfers(self, status: str | None = None, pod_key: str | None = None):
        where, args = [], []
        if status:
            where.append("t.status=?")
            args.append(status)
        if pod_key:
            pod = self._load_pod(pod_key)
            where.append("t.pod_id=?")
            args.append(pod["id"])
        sql = ("SELECT t.* FROM custody_transfer t"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY t.created_at DESC")
        return [self.transfer_dict(r) for r in self.s.q_all(sql, tuple(args))]

    def list_todo(self, person_id: str | None = None):
        """当前自然人待办：待其确认的交接、待检查入库、待双角色解除调查。"""
        out = {"transfers": [], "inspections": [], "investigations": []}
        person = self._load_person(person_id, "操作自然人") if person_id else None
        person_roles = person["roles"] if person else []
        rows = self.s.q_all(
            "SELECT * FROM custody_transfer WHERE status='PENDING' AND deadline_at>=?"
            " ORDER BY deadline_at", (utcnow(),))
        for r in rows:
            d = self.transfer_dict(r)
            sides = {x["side"] for x in d["confirmations"]}
            can_receive = (not r["receiver_person"] and
                           any(role in person_roles for role in
                               ("LINE",) if r["direction"] == "OUTBOUND")
                           ) or (not r["receiver_person"] and
                                 any(role in person_roles for role in WAREHOUSE_ROLES)
                                 and r["direction"] == "INBOUND")
            if person_id == r["surrender_person"] or person_id == r["receiver_person"] or can_receive:
                needed = "SURRENDER" if "SURRENDER" not in sides and person_id == r["surrender_person"] else ""
                if not needed and "RECEIVER" not in sides and (
                        person_id == r["receiver_person"] or can_receive):
                    needed = "RECEIVER"
                if needed:
                    d["my_side"] = needed
                    out["transfers"].append(d)
        if not person_id or any(r in person_roles for r in WAREHOUSE_ROLES):
            for p in self.s.q_all(
                    "SELECT * FROM custody_pod WHERE status='AWAITING_INSPECTION'"):
                out["inspections"].append(self._pod_dict(p))
        for inv in self.s.q_all(
                "SELECT * FROM custody_investigation WHERE status='OPEN' ORDER BY opened_at"):
            d = self.investigation_dict(inv)
            roles_done = {x["role"] for x in d["confirmations"]}
            remaining = [r for r in ("SAFETY", "PROCESS") if r not in roles_done]
            if not person_id or any(r in person_roles for r in remaining):
                out["investigations"].append(d)
        return out

    def custody_history(self, key: str) -> dict[str, Any]:
        pod = self._load_pod(key)
        rels = [dict(r) for r in self.s.q_all(
            "SELECT * FROM custody_relation WHERE pod_id=? ORDER BY id", (pod["id"],))]
        for r in rels:
            r["detail"] = json.loads(r.pop("detail_json") or "{}")
        transfers = self.list_transfers(pod_key=pod["id"])
        investigations = [self.investigation_dict(r) for r in self.s.q_all(
            "SELECT * FROM custody_investigation WHERE pod_id=? ORDER BY opened_at",
            (pod["id"],))]
        return {"pod": self._pod_dict(pod), "relations": rels,
                "transfers": transfers, "investigations": investigations}

    def list_exceptions(self):
        transfers = [self.transfer_dict(r) for r in self.s.q_all(
            "SELECT * FROM custody_transfer WHERE status IN ('REJECTED','EXPIRED')"
            " ORDER BY created_at DESC")]
        inspections = [dict(r) for r in self.s.q_all(
            "SELECT * FROM custody_inspection WHERE result='LABEL_DAMAGED' ORDER BY at DESC")]
        investigations = [self.investigation_dict(r) for r in self.s.q_all(
            "SELECT * FROM custody_investigation ORDER BY opened_at DESC")]
        return {"rejected_or_expired_transfers": transfers,
                "damaged_label_inspections": inspections,
                "investigations": investigations}

    def assert_set_issued_to_line(self, set_id: str):
        pod = self.s.q_one("SELECT * FROM custody_pod WHERE set_id=?", (set_id,))
        if pod and pod["status"] != "ISSUED_TO_LINE":
            raise DomainError("POD_NOT_ISSUED",
                              "实体版匣尚未完成出库双方确认，不得发给生产线曝光",
                              409, {"pod_id": pod["id"], "pod_status": pod["status"],
                                    "label_no": pod["label_no"]})

    def migrate_set_pod(self, c, old_set_id: str, new_set_id: str):
        c.execute("UPDATE custody_pod SET set_id=? WHERE set_id=?",
                  (new_set_id, old_set_id))

    # ------------------------------------------------------------ 启动恢复
    def recover_on_startup(self) -> dict[str, Any]:
        """接续未过期 PENDING；仅将已超过确认期限的交接冻结归还原责任人。"""
        expired_transfers: list[str] = []
        with self.s.tx() as c:
            pending = c.execute(
                "SELECT * FROM custody_transfer WHERE status='PENDING'").fetchall()
            for t in pending:
                if t["deadline_at"] < utcnow():
                    if self._expire_if_due(c, t):
                        expired_transfers.append(t["id"])
        if expired_transfers:
            with self.s.tx() as c:
                self.s.audit(c, "system", "custody.startup.recover", None,
                             detail={"expired_transfers": expired_transfers})
        return {"expired_transfers": expired_transfers}
