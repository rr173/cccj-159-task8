"""实体掩模版匣保管交接链：库房登记、出库/入库双方确认、检查与调查。

门禁一览（每一项都是显式拒收 + 留痕，被拒申请仍落 handoff 表）：
  HANDOFF_TAG_NOT_FOUND      标签号码错误（查无此版匣）
  HANDOFF_PERSON_NO_DUTY     交出者/接收者没有 CUSTODIAN 保管责任职责
  HANDOFF_SAME_PERSON        交出者与接收者必须是不同自然人
  HANDOFF_FROZEN_BY_OTHER    版匣已被另一笔未决交接冻结
  HANDOFF_DEADLINE_PASSED    确认期限已过（申请时或确认时裁决）
  POD_UNDER_INVESTIGATION    调查中：禁止出库/再次出库
  HANDOFF_NOT_DESIGNATED     非该笔交接指定的交出者/接收者
  HANDOFF_SIDE_TAKEN         同一侧确认权已被先完成者取得
  HANDOFF_GIVER_NOT_HOLDER   交出者不是版匣当前责任人
  HANDOFF_PARTY_LOCKED       已有一方确认后禁止替换交接人员
  POD_HANDOFF_NOT_CONFIRMED  双方确认结束之前不得把版匣发给生产线
  INVESTIGATION_SAME_PERSON  解除调查必须是两个不同自然人
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone

from .domain import DomainError
from .store import utcnow

SIDES = ("GIVER", "RECEIVER")
DIRECTIONS = ("OUTBOUND", "INBOUND")


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


class CustodyMixin:
    """被 Service 继承：self.s 为 Store，self.s.tx()/q_one/q_all/audit 已就绪。"""

    # ================================================================ 登记
    def register_pod(self, actor_id: str, tag_no: str, cabinet: str,
                     responsible_person: str, set_key: str | None = None,
                     op_id: str | None = None) -> dict:
        """库房登记防拆标签、柜位和责任人。"""
        actor = self.require_role(actor_id, "KEEPER")
        tag_no = (tag_no or "").strip()
        cabinet = (cabinet or "").strip()
        if not tag_no:
            raise DomainError("BAD_REQUEST", "tag_no 防拆标签号码必填", 400)
        holder = self.require_person(responsible_person)
        if "CUSTODIAN" not in holder["roles"]:
            raise DomainError("HANDOFF_PERSON_NO_DUTY",
                              f"{holder['name']} 不具备 CUSTODIAN 保管责任职责", 422,
                              {"person": responsible_person})
        set_id = None
        if set_key:
            set_id = self._load_set(set_key)["id"]
        payload = {"tag_no": tag_no, "cabinet": cabinet,
                   "responsible_person": responsible_person, "set_id": set_id}

        def action():
            with self.s.tx() as c:
                if c.execute("SELECT 1 FROM pod WHERE tag_no=?", (tag_no,)).fetchone():
                    raise DomainError("POD_TAG_EXISTS",
                                      f"防拆标签 {tag_no} 已登记", 409)
                pod_id = "POD-" + secrets.token_hex(5)
                c.execute(
                    "INSERT INTO pod(id,tag_no,cabinet,responsible_person,set_id,status,"
                    "created_by,created_at) VALUES(?,?,?,?,?, 'IN_STORE',?,?)",
                    (pod_id, tag_no, cabinet, responsible_person, set_id,
                     actor["id"], utcnow()))
                c.execute(
                    "INSERT INTO custody_record(pod_id,holder_person,cabinet,reason,"
                    "started_at) VALUES(?,?,?, 'REGISTER',?)",
                    (pod_id, responsible_person, cabinet, utcnow()))
                self.s.audit(c, actor["id"], "pod.register", pod_id, op_id=op_id,
                             detail={"tag_no": tag_no, "cabinet": cabinet,
                                     "responsible_person": responsible_person})
                return {"pod_id": pod_id, "tag_no": tag_no, "cabinet": cabinet,
                        "responsible_person": responsible_person,
                        "status": "IN_STORE"}
        return self.idempotent(op_id, "pod.register", payload, action,
                               actor=actor["id"])

    def list_pods(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM pod"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY created_at"
        return [dict(r) for r in self.s.q_all(sql, args)]

    def _load_pod(self, c, key: str):
        return c.execute("SELECT * FROM pod WHERE id=? OR tag_no=?",
                         (key, key)).fetchone()

    def get_pod(self, key: str) -> dict:
        with self.s.tx() as c:
            pod = self._load_pod(c, key)
            if not pod:
                raise DomainError("NOT_FOUND", f"版匣不存在：{key}", 404)
            return self._pod_dict(c, pod)

    def _pod_dict(self, c, pod) -> dict:
        d = dict(pod)
        holder = c.execute("SELECT name FROM person WHERE id=?",
                           (pod["responsible_person"],)).fetchone()
        d["responsible_name"] = holder["name"] if holder else None
        d["custody_chain"] = [dict(r) for r in c.execute(
            "SELECT * FROM custody_record WHERE pod_id=? ORDER BY id", (pod["id"],))]
        active = c.execute(
            "SELECT id,direction,status,deadline_ts FROM handoff"
            " WHERE pod_id=? AND status='PENDING'", (pod["id"],)).fetchall()
        d["active_handoffs"] = [dict(r) for r in active]
        inv = c.execute(
            "SELECT * FROM investigation WHERE pod_id=? AND status='OPEN' LIMIT 1",
            (pod["id"],)).fetchone()
        d["open_investigation"] = dict(inv) if inv else None
        return d

    # ================================================================ 申请交接
    def request_handoff(self, actor_id: str, tag_no: str, direction: str,
                        giver_id: str, receiver_id: str,
                        deadline_ts: str | None = None, cabinet: str | None = None,
                        note: str = "", op_id: str | None = None) -> dict:
        """扫码工作台申请出库/入库交接。每笔只认一个接收者；
        标签错误、无职责、被另笔冻结、期限已过都留下申请并给明确拒绝原因。"""
        actor = self.require_person(actor_id)
        if "KEEPER" not in actor["roles"] and "CUSTODIAN" not in actor["roles"]:
            raise DomainError("UNAUTHORIZED",
                              "只有库房管理员（KEEPER）或保管责任人（CUSTODIAN）可申请交接",
                              403)
        direction = (direction or "").upper()
        if direction not in DIRECTIONS:
            raise DomainError("BAD_REQUEST", "direction 必须是 OUTBOUND/INBOUND", 400)
        tag_no = (tag_no or "").strip()
        if not tag_no:
            raise DomainError("BAD_REQUEST", "tag_no 必填", 400)
        cabinet = (cabinet or "").strip()
        payload = {"tag_no": tag_no, "direction": direction, "giver": giver_id,
                   "receiver": receiver_id, "deadline": deadline_ts,
                   "cabinet": cabinet, "note": note}

        def action():
            # 期限：显式给出则校验，否则按服务默认 TTL
            if deadline_ts:
                try:
                    dl = _parse_ts(deadline_ts)
                except (ValueError, AttributeError):
                    raise DomainError("BAD_TIMESTAMP", "deadline_ts 必须是 ISO-8601 UTC", 400)
            else:
                dl = _parse_ts(utcnow()) + timedelta(seconds=self.handoff_ttl)
            deadline = deadline_ts or dl.strftime("%Y-%m-%dT%H:%M:%S.000Z")

            with self.s.tx() as c:
                pod = c.execute("SELECT * FROM pod WHERE tag_no=?",
                                (tag_no,)).fetchone()
                pod_id = pod["id"] if pod else None

                # 期限已过：先于其它门禁（申请都不许再立）
                if deadline_ts and dl <= _parse_ts(utcnow()):
                    return self._reject_handoff_at_request(
                        pod_id, tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "HANDOFF_DEADLINE_PASSED",
                        f"确认期限 {deadline_ts} 已过，不能再申请交接", conn=c)

                # 标签号码错误：申请仍落库（pod_id 为 NULL），不带冻结
                if not pod:
                    return self._reject_handoff_at_request(
                        None, tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "HANDOFF_TAG_NOT_FOUND",
                        f"标签号码 {tag_no} 查无已登记版匣", conn=c)

                # 人员职责：交出者与接收者都必须是保管责任人，且为不同自然人
                for field, pid in (("giver", giver_id), ("receiver", receiver_id)):
                    p = c.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
                    if not p or "CUSTODIAN" not in json.loads(p["roles"]):
                        return self._reject_handoff_at_request(
                            pod_id, tag_no, direction, giver_id, receiver_id, deadline,
                            cabinet, note, actor, "HANDOFF_PERSON_NO_DUTY",
                            f"{field}={pid} 不是具备 CUSTODIAN 职责的保管责任人",
                            conn=c, detail={"field": field, "person": pid})
                if giver_id == receiver_id:
                    return self._reject_handoff_at_request(
                        pod_id, tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "HANDOFF_SAME_PERSON",
                        "交出者与接收者必须是不同自然人", conn=c)

                if pod["status"] == "UNDER_INVESTIGATION":
                    return self._reject_handoff_at_request(
                        pod["id"], tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "POD_UNDER_INVESTIGATION",
                        "版匣处于调查中，禁止出库/再次出库", conn=c)
                other = c.execute(
                    "SELECT id FROM handoff WHERE pod_id=? AND status='PENDING' LIMIT 1",
                    (pod["id"],)).fetchone()
                if other:
                    return self._reject_handoff_at_request(
                        pod["id"], tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "HANDOFF_FROZEN_BY_OTHER",
                        f"版匣已被交接申请 {other['id']} 冻结", conn=c,
                        detail={"freezing_handoff": other["id"]})
                if direction == "OUTBOUND":
                    if pod["status"] != "IN_STORE":
                        return self._reject_handoff_at_request(
                            pod["id"], tag_no, direction, giver_id, receiver_id,
                            deadline, cabinet, note, actor, "POD_NOT_IN_STORE",
                            f"出库要求版匣在库（当前 {pod['status']}）", conn=c)
                elif pod["status"] != "IN_TRANSIT":
                    return self._reject_handoff_at_request(
                        pod["id"], tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "POD_NOT_IN_TRANSIT",
                        f"入库要求版匣在流转中（当前 {pod['status']}）", conn=c)
                if pod["responsible_person"] != giver_id:
                    return self._reject_handoff_at_request(
                        pod["id"], tag_no, direction, giver_id, receiver_id, deadline,
                        cabinet, note, actor, "HANDOFF_GIVER_NOT_HOLDER",
                        f"交出者 {giver_id} 不是版匣当前责任人 "
                        f"{pod['responsible_person']}", conn=c,
                        detail={"current_holder": pod["responsible_person"]})

                hid = "HND-" + secrets.token_hex(6)
                c.execute(
                    "INSERT INTO handoff(id,pod_id,tag_no,direction,giver_id,receiver_id,"
                    "requested_by,requested_at,status,deadline_ts,cabinet,note)"
                    " VALUES(?,?,?,?,?,?,?,?, 'PENDING',?,?,?)",
                    (hid, pod["id"], tag_no, direction, giver_id, receiver_id,
                     actor["id"], utcnow(), deadline, cabinet, note))
                c.execute("UPDATE pod SET frozen_by_handoff=? WHERE id=?",
                          (hid, pod["id"]))
                self.s.audit(c, actor["id"], "handoff.request", hid, op_id=op_id,
                             detail={"pod": pod["id"], "tag_no": tag_no,
                                     "direction": direction,
                                     "giver": giver_id, "receiver": receiver_id,
                                     "deadline_ts": deadline})
                return {"handoff_id": hid, "pod_id": pod["id"], "tag_no": tag_no,
                        "direction": direction, "giver_id": giver_id,
                        "receiver_id": receiver_id, "status": "PENDING",
                        "deadline_ts": deadline,
                        "requires": ["GIVER", "RECEIVER"]}
        return self.idempotent(op_id, "handoff.request", payload, action,
                               actor=actor["id"])

    def _reject_handoff_at_request(self, pod_id, tag_no, direction, giver_id,
                                   receiver_id, deadline, cabinet, note, actor,
                                   code, message, conn=None, detail=None):
        """申请阶段被拒也要留下申请（status=REJECTED + 明确拒绝原因）。"""
        def write(c):
            hid = "HND-" + secrets.token_hex(6)
            c.execute(
                "INSERT INTO handoff(id,pod_id,tag_no,direction,giver_id,receiver_id,"
                "requested_by,requested_at,status,reject_code,reject_message,"
                "deadline_ts,cabinet,note,completed_at)"
                " VALUES(?,?,?,?,?,?,?,?,'REJECTED',?,?,?,?,?,?)",
                (hid, pod_id, tag_no, direction, giver_id, receiver_id,
                 actor["id"], utcnow(), code, message, deadline, cabinet or "",
                 note, utcnow()))
            self.s.audit(c, actor["id"], "handoff.request_rejected", hid,
                         detail={"error": code, "pod": pod_id, "tag_no": tag_no,
                                 "direction": direction, **(detail or {})})
            return {"handoff_id": hid, "pod_id": pod_id, "tag_no": tag_no,
                    "direction": direction, "giver_id": giver_id,
                    "receiver_id": receiver_id, "status": "REJECTED",
                    "reject_code": code, "reject_message": message,
                    "deadline_ts": deadline}
        if conn is not None:
            return write(conn)
        with self.s.tx() as c:
            return write(c)

    # ================================================================ 双方确认
    def confirm_handoff(self, actor_id: str, handoff_id: str, side: str,
                        note: str = "", inspection: str | None = None,
                        op_id: str | None = None) -> dict:
        """交出者与接收者分开确认。同一侧先完成者唯一取得确认权；
        第二签完成时移交保管权。晚到的确认不能推翻既有决定。"""
        actor = self.require_role(actor_id, "CUSTODIAN")
        side = (side or "").upper()
        if side not in SIDES:
            raise DomainError("BAD_REQUEST", "side 必须是 GIVER/RECEIVER", 400)
        if inspection and inspection not in ("OK", "TAG_DAMAGED"):
            raise DomainError("BAD_REQUEST", "inspection 必须是 OK/TAG_DAMAGED", 400)
        payload = {"handoff_id": handoff_id, "side": side, "by": actor["id"],
                   "note": note, "inspection": inspection}

        def action():
            # 确认期限：动作发生时裁决；过期需先在独立事务提交 EXPIRED + 解冻，
            # 再抛出拒收（否则同事务回滚会把过期裁决一并撤销）
            row = self.s.q_one("SELECT * FROM handoff WHERE id=?", (handoff_id,))
            if row is not None and row["status"] == "PENDING" and \
                    row["deadline_ts"] <= utcnow():
                with self.s.tx() as c:
                    self._expire_handoff(c, row)
                raise DomainError("HANDOFF_DEADLINE_PASSED",
                                  f"确认期限 {row['deadline_ts']} 已过", 409,
                                  {"handoff_id": handoff_id,
                                   "deadline_ts": row["deadline_ts"]})
            with self.s.tx() as c:
                h = c.execute("SELECT * FROM handoff WHERE id=?",
                              (handoff_id,)).fetchone()
                if not h:
                    raise DomainError("NOT_FOUND", "交接申请不存在", 404)
                if h["status"] == "REJECTED":
                    raise DomainError(h["reject_code"] or "HANDOFF_REJECTED",
                                      f"该申请在登记阶段已被拒绝：{h['reject_message']}",
                                      409, {"handoff_id": handoff_id,
                                            "reject_code": h["reject_code"]})
                if h["status"] in ("CONFIRMED", "CANCELLED", "EXPIRED"):
                    raise DomainError("HANDOFF_DECIDED",
                                      f"交接已 {h['status']}，晚到的确认不得推翻既有决定",
                                      409, {"handoff_id": handoff_id,
                                            "status": h["status"]})
                designated = h["giver_id"] if side == "GIVER" else h["receiver_id"]
                if actor["id"] != designated:
                    raise DomainError("HANDOFF_NOT_DESIGNATED",
                                      f"该笔交接的{side}只能由 {designated} 确认",
                                      403, {"handoff_id": handoff_id, "side": side,
                                            "designated": designated})
                prior = c.execute(
                    "SELECT side,person_id FROM handoff_confirmation WHERE handoff_id=?",
                    (handoff_id,)).fetchall()
                # 主键 (handoff_id, side) 兜底并发：仅先完成者取得该侧确认权
                try:
                    c.execute(
                        "INSERT INTO handoff_confirmation(handoff_id,side,person_id,at,note)"
                        " VALUES(?,?,?,?,?)",
                        (handoff_id, side, actor["id"], utcnow(), note))
                except Exception:
                    winner = c.execute(
                        "SELECT person_id FROM handoff_confirmation WHERE handoff_id=? AND side=?",
                        (handoff_id, side)).fetchone()
                    raise DomainError("HANDOFF_SIDE_TAKEN",
                                      f"{side} 侧确认权已由 {winner['person_id']} 先完成取得",
                                      409, {"handoff_id": handoff_id, "side": side,
                                            "winner": winner["person_id"]})
                self.s.audit(c, actor["id"], "handoff.confirm", handoff_id,
                             op_id=op_id, detail={"side": side})
                done = {p["side"]: p["person_id"] for p in prior}
                done[side] = actor["id"]
                if len(done) < 2:
                    return {"handoff_id": handoff_id, "status": "PENDING",
                            "confirmations": [{"side": k, "person_id": v}
                                             for k, v in done.items()],
                            "requires": [s for s in SIDES if s not in done]}

                # 双方确认齐备 → 移交保管权（调查中的版匣仍不得出库）
                pod = c.execute("SELECT * FROM pod WHERE id=?", (h["pod_id"],)).fetchone()
                if pod["status"] == "UNDER_INVESTIGATION":
                    raise DomainError("POD_UNDER_INVESTIGATION",
                                      "版匣在确认期间转入调查，交接不能完成", 409)
                self._complete_handoff(c, h, pod)
                result = {"handoff_id": handoff_id, "pod_id": pod["id"],
                          "tag_no": pod["tag_no"], "direction": h["direction"],
                          "giver_id": h["giver_id"], "receiver_id": h["receiver_id"],
                          "status": "CONFIRMED",
                          "new_holder": h["receiver_id"],
                          "pod_status": "IN_TRANSIT" if h["direction"] == "OUTBOUND"
                                        else "IN_STORE"}
                # 入库检查随接收确认提交：标签破损 → 转入调查并禁止再次出库
                if h["direction"] == "INBOUND" and inspection == "TAG_DAMAGED":
                    inv = self._open_investigation(
                        c, pod, "TAG_DAMAGED",
                        f"入库检查发现标签破损：{note or '防拆标签破损'}",
                        actor["id"], handoff_id=handoff_id)
                    c.execute(
                        "INSERT INTO inspection(id,pod_id,handoff_id,inspector,result,"
                        "note,at) VALUES(?,?,?,?, 'TAG_DAMAGED',?,?)",
                        ("INS-" + secrets.token_hex(5), pod["id"], handoff_id,
                         actor["id"], note, utcnow()))
                    result["inspection"] = "TAG_DAMAGED"
                    result["investigation_id"] = inv["investigation_id"]
                    result["pod_status"] = "UNDER_INVESTIGATION"
                elif inspection:
                    c.execute(
                        "INSERT INTO inspection(id,pod_id,handoff_id,inspector,result,"
                        "note,at) VALUES(?,?,?,?,?,?,?)",
                        ("INS-" + secrets.token_hex(5), pod["id"], handoff_id,
                         actor["id"], inspection, note, utcnow()))
                    result["inspection"] = inspection
                return result
        return self.idempotent(op_id, "handoff.confirm", payload, action,
                               actor=actor["id"])

    def _complete_handoff(self, c, h, pod):
        now = utcnow()
        new_status = "IN_TRANSIT" if h["direction"] == "OUTBOUND" else "IN_STORE"
        cabinet = pod["cabinet"]
        if h["direction"] == "INBOUND" and h["cabinet"]:
            cabinet = h["cabinet"]
        c.execute(
            "UPDATE pod SET responsible_person=?,status=?,frozen_by_handoff=?,"
            "cabinet=?,line_released_at=? WHERE id=?",
            (h["receiver_id"], new_status, None, cabinet,
             None if h["direction"] == "INBOUND" else pod["line_released_at"],
             pod["id"]))
        c.execute("UPDATE custody_record SET ended_at=? WHERE pod_id=? AND ended_at IS NULL",
                  (now, pod["id"]))
        c.execute(
            "INSERT INTO custody_record(pod_id,holder_person,cabinet,handoff_id,reason,"
            "started_at) VALUES(?,?,?,?,?,?)",
            (pod["id"], h["receiver_id"],
             cabinet if h["direction"] == "INBOUND" else "", h["id"],
             h["direction"], now))
        c.execute("UPDATE handoff SET status='CONFIRMED',completed_at=? WHERE id=?",
                  (now, h["id"]))
        self.s.audit(c, h["receiver_id"], "handoff.complete", h["id"],
                     detail={"pod": pod["id"], "direction": h["direction"],
                             "new_holder": h["receiver_id"], "pod_status": new_status})

    def _expire_handoff(self, c, h):
        """确认期限已过：申请置 EXPIRED，清除版匣上的失效冻结。"""
        c.execute("UPDATE handoff SET status='EXPIRED' WHERE id=? AND status='PENDING'",
                  (h["id"],))
        c.execute("UPDATE pod SET frozen_by_handoff=NULL WHERE frozen_by_handoff=?",
                  (h["id"],))
        self.s.audit(c, "system", "handoff.expire", h["id"],
                     detail={"pod": h["pod_id"], "deadline_ts": h["deadline_ts"]})

    # ================================================================ 撤销 / 换人
    def cancel_handoff(self, actor_id: str, handoff_id: str,
                       op_id: str | None = None) -> dict:
        """撤销仅允许申请人或库房管理员；已有一方确认后禁止撤销。"""
        actor = self.require_person(actor_id)
        payload = {"handoff_id": handoff_id, "by": actor["id"]}

        def action():
            with self.s.tx() as c:
                h = c.execute("SELECT * FROM handoff WHERE id=?",
                              (handoff_id,)).fetchone()
                if not h:
                    raise DomainError("NOT_FOUND", "交接申请不存在", 404)
                if ("KEEPER" not in actor["roles"]
                        and h["requested_by"] != actor["id"]):
                    raise DomainError("UNAUTHORIZED",
                                      "撤销仅允许申请人或库房管理员（KEEPER）执行", 403)
                if h["status"] in ("CONFIRMED", "CANCELLED", "REJECTED", "EXPIRED"):
                    raise DomainError("HANDOFF_DECIDED",
                                      f"交接已 {h['status']}，再次撤销不得推翻既有决定",
                                      409, {"handoff_id": handoff_id,
                                            "status": h["status"]})
                confirmed = c.execute(
                    "SELECT COUNT(1) AS n FROM handoff_confirmation WHERE handoff_id=?",
                    (handoff_id,)).fetchone()["n"]
                if confirmed:
                    raise DomainError("HANDOFF_CONFIRMATION_EXISTS",
                                      f"已有 {confirmed} 方确认，禁止撤销/替换交接人员",
                                      409)
                c.execute("UPDATE handoff SET status='CANCELLED',completed_at=? WHERE id=?",
                          (utcnow(), handoff_id))
                c.execute("UPDATE pod SET frozen_by_handoff=NULL WHERE frozen_by_handoff=?",
                          (handoff_id,))
                self.s.audit(c, actor["id"], "handoff.cancel", handoff_id,
                             op_id=op_id, detail={"pod": h["pod_id"]})
                return {"handoff_id": handoff_id, "status": "CANCELLED"}
        return self.idempotent(op_id, "handoff.cancel", payload, action,
                               actor=actor["id"])

    def amend_handoff(self, actor_id: str, handoff_id: str, giver_id: str,
                      receiver_id: str, deadline_ts: str | None = None,
                      op_id: str | None = None) -> dict:
        """双方确认开始之前可替换交出者/接收者；已有一方确认即锁定。"""
        actor = self.require_person(actor_id)
        payload = {"handoff_id": handoff_id, "giver": giver_id,
                   "receiver": receiver_id, "deadline": deadline_ts}

        def action():
            with self.s.tx() as c:
                h = c.execute("SELECT * FROM handoff WHERE id=?",
                              (handoff_id,)).fetchone()
                if not h:
                    raise DomainError("NOT_FOUND", "交接申请不存在", 404)
                if ("KEEPER" not in actor["roles"]
                        and h["requested_by"] != actor["id"]):
                    raise DomainError("UNAUTHORIZED",
                                      "替换交接人员仅允许申请人或库房管理员（KEEPER）", 403)
                if h["status"] != "PENDING":
                    raise DomainError("HANDOFF_DECIDED",
                                      f"交接已 {h['status']}，不能替换人员", 409)
                confirmed = c.execute(
                    "SELECT COUNT(1) AS n FROM handoff_confirmation WHERE handoff_id=?",
                    (handoff_id,)).fetchone()["n"]
                if confirmed:
                    raise DomainError("HANDOFF_PARTY_LOCKED",
                                      f"已有 {confirmed} 方确认，禁止替换交接人员", 409)
                for field, pid in (("giver", giver_id), ("receiver", receiver_id)):
                    p = c.execute("SELECT * FROM person WHERE id=?", (pid,)).fetchone()
                    if not p or "CUSTODIAN" not in json.loads(p["roles"]):
                        raise DomainError("HANDOFF_PERSON_NO_DUTY",
                                          f"{field}={pid} 不具备 CUSTODIAN 职责", 422)
                if giver_id == receiver_id:
                    raise DomainError("HANDOFF_SAME_PERSON",
                                      "交出者与接收者必须是不同自然人", 422)
                pod = c.execute("SELECT * FROM pod WHERE id=?", (h["pod_id"],)).fetchone()
                if h["direction"] == "OUTBOUND" and pod["responsible_person"] != giver_id:
                    raise DomainError("HANDOFF_GIVER_NOT_HOLDER",
                                      f"交出者 {giver_id} 不是版匣当前责任人", 422)
                deadline = h["deadline_ts"]
                if deadline_ts:
                    try:
                        dl = _parse_ts(deadline_ts)
                    except (ValueError, AttributeError):
                        raise DomainError("BAD_TIMESTAMP", "deadline_ts 必须是 ISO-8601", 400)
                    if dl <= _parse_ts(utcnow()):
                        raise DomainError("HANDOFF_DEADLINE_PASSED",
                                          f"新确认期限 {deadline_ts} 已过", 422)
                    deadline = deadline_ts
                c.execute(
                    "UPDATE handoff SET giver_id=?,receiver_id=?,deadline_ts=? WHERE id=?",
                    (giver_id, receiver_id, deadline, handoff_id))
                self.s.audit(c, actor["id"], "handoff.amend", handoff_id,
                             op_id=op_id, detail={"giver": giver_id,
                                                  "receiver": receiver_id})
                return {"handoff_id": handoff_id, "status": "PENDING",
                        "giver_id": giver_id, "receiver_id": receiver_id,
                        "deadline_ts": deadline}
        return self.idempotent(op_id, "handoff.amend", payload, action,
                               actor=actor["id"])

    # ================================================================ 查询
    def list_handoffs(self, status: str | None = None, pod: str | None = None,
                      person: str | None = None) -> list[dict]:
        sql = ("SELECT DISTINCT h.* FROM handoff h"
               " LEFT JOIN handoff_confirmation c ON c.handoff_id=h.id WHERE 1=1")
        args: list = []
        if status:
            sql += " AND h.status=?"
            args.append(status)
        if pod:
            sql += " AND (h.pod_id=? OR h.tag_no=?)"
            args += [pod, pod]
        if person:
            sql += " AND (h.giver_id=? OR h.receiver_id=? OR c.person_id=?)"
            args += [person, person, person]
        sql += " ORDER BY h.requested_at DESC"
        out = []
        for r in self.s.q_all(sql, tuple(args)):
            d = dict(r)
            d["confirmations"] = [dict(x) for x in self.s.q_all(
                "SELECT side,person_id,at FROM handoff_confirmation WHERE handoff_id=?"
                " ORDER BY at", (r["id"],))]
            out.append(d)
        return out

    def list_todos(self, person_id: str) -> list[dict]:
        """待办：未决交接中该自然人需要确认但尚未确认的一侧。"""
        rows = self.s.q_all(
            "SELECT h.* FROM handoff h WHERE h.status='PENDING' AND h.deadline_ts>?"
            " AND (h.giver_id=? OR h.receiver_id=?) ORDER BY h.deadline_ts",
            (utcnow(), person_id, person_id))
        out = []
        for h in rows:
            done = {r["side"] for r in self.s.q_all(
                "SELECT side FROM handoff_confirmation WHERE handoff_id=?", (h["id"],))}
            waiting = []
            if h["giver_id"] == person_id and "GIVER" not in done:
                waiting.append("GIVER")
            if h["receiver_id"] == person_id and "RECEIVER" not in done:
                waiting.append("RECEIVER")
            if waiting:
                d = dict(h)
                d["waiting_for_me"] = waiting
                out.append(d)
        return out

    def custody_timeline(self, key: str) -> dict:
        with self.s.tx() as c:
            pod = self._load_pod(c, key)
            if not pod:
                raise DomainError("NOT_FOUND", f"版匣不存在：{key}", 404)
            records = [dict(r) for r in c.execute(
                "SELECT * FROM custody_record WHERE pod_id=? ORDER BY id", (pod["id"],))]
            for rec in records:
                p = c.execute("SELECT name FROM person WHERE id=?",
                              (rec["holder_person"],)).fetchone()
                rec["holder_name"] = p["name"] if p else None
            return {"pod_id": pod["id"], "tag_no": pod["tag_no"],
                    "current_holder": pod["responsible_person"],
                    "status": pod["status"], "chain": records}

    def custody_exceptions(self) -> dict:
        """异常详情：被拒/过期交接、标签破损检查、调查。"""
        rejected = [dict(r) for r in self.s.q_all(
            "SELECT * FROM handoff WHERE status IN ('REJECTED','EXPIRED')"
            " ORDER BY requested_at DESC")]
        damaged = [dict(r) for r in self.s.q_all(
            "SELECT * FROM inspection WHERE result='TAG_DAMAGED' ORDER BY at DESC")]
        investigations = [dict(r) for r in self.s.q_all(
            "SELECT * FROM investigation ORDER BY opened_at DESC")]
        return {"rejected_handoffs": rejected, "damaged_inspections": damaged,
                "investigations": investigations}

    # ================================================================ 发给生产线
    def release_pod_to_line(self, actor_id: str, key: str,
                            op_id: str | None = None) -> dict:
        """双方确认结束之前不得把版匣发给生产线。"""
        actor = self.require_role(actor_id, "KEEPER")
        payload = {"pod": key}

        def action():
            with self.s.tx() as c:
                pod = self._load_pod(c, key)
                if not pod:
                    raise DomainError("NOT_FOUND", f"版匣不存在：{key}", 404)
                if pod["status"] == "UNDER_INVESTIGATION":
                    raise DomainError("POD_UNDER_INVESTIGATION",
                                      "调查中的版匣禁止发往生产线", 409)
                # 双方确认结束之前不得发线：未决冻结或无已确认出库交接都拦在同一门禁
                last = c.execute(
                    "SELECT * FROM handoff WHERE pod_id=? AND direction='OUTBOUND'"
                    " ORDER BY CASE status WHEN 'CONFIRMED' THEN 0 ELSE 1 END,"
                    " requested_at DESC, id DESC LIMIT 1", (pod["id"],)).fetchone()
                if pod["frozen_by_handoff"] or (
                        not last or last["status"] != "CONFIRMED"):
                    raise DomainError("POD_HANDOFF_NOT_CONFIRMED",
                                      "出库交接未经交出者与接收者双方确认结束，"
                                      "不得把版匣发给生产线", 409,
                                      {"frozen_by_handoff": pod["frozen_by_handoff"]})
                if pod["line_released_at"]:
                    return {"pod_id": pod["id"], "line_released_at":
                            pod["line_released_at"], "noop": True}
                now = utcnow()
                c.execute("UPDATE pod SET line_released_at=? WHERE id=?", (now, pod["id"]))
                self.s.audit(c, actor["id"], "pod.release_line", pod["id"],
                             op_id=op_id, detail={"handoff": last["id"]})
                return {"pod_id": pod["id"], "tag_no": pod["tag_no"],
                        "line_released_at": now}
        return self.idempotent(op_id, "pod.release_line", payload, action,
                               actor=actor["id"])

    # ================================================================ 入库检查 / 调查
    def inspect_pod(self, actor_id: str, key: str, result: str, note: str = "",
                    op_id: str | None = None) -> dict:
        """入库检查（库房执行）。标签 OK 仅留痕；标签破损 → 转入调查并禁止再次出库。"""
        actor = self.require_role(actor_id, "KEEPER")
        if result not in ("OK", "TAG_DAMAGED"):
            raise DomainError("BAD_REQUEST", "result 必须是 OK/TAG_DAMAGED", 400)
        payload = {"pod": key, "result": result, "note": note}

        def action():
            with self.s.tx() as c:
                pod = self._load_pod(c, key)
                if not pod:
                    raise DomainError("HANDOFF_TAG_NOT_FOUND",
                                      f"标签 {key} 查无已登记版匣", 404)
                iid = "INS-" + secrets.token_hex(5)
                c.execute(
                    "INSERT INTO inspection(id,pod_id,handoff_id,inspector,result,note,at)"
                    " VALUES(?,?,NULL,?,?,?,?)",
                    (iid, pod["id"], actor["id"], result, note, utcnow()))
                self.s.audit(c, actor["id"], "pod.inspect", pod["id"], op_id=op_id,
                             detail={"result": result, "inspection": iid})
                out = {"inspection_id": iid, "pod_id": pod["id"], "result": result}
                if result == "TAG_DAMAGED":
                    existing = c.execute(
                        "SELECT * FROM investigation WHERE pod_id=? AND status='OPEN'",
                        (pod["id"],)).fetchone()
                    if existing:
                        out["investigation_id"] = existing["id"]
                        out["already_under_investigation"] = True
                    else:
                        inv = self._open_investigation(
                            c, pod, "TAG_DAMAGED",
                            f"入库检查发现标签破损：{note or '防拆标签破损'}",
                            actor["id"])
                        out["investigation_id"] = inv["investigation_id"]
                        out["pod_status"] = "UNDER_INVESTIGATION"
                return out
        return self.idempotent(op_id, "pod.inspect", payload, action,
                               actor=actor["id"])

    def list_inspections(self, pod: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM inspection", []
        if pod:
            sql += " WHERE pod_id IN (SELECT id FROM pod WHERE id=? OR tag_no=?)"
            args += [pod, pod]
        sql += " ORDER BY at DESC"
        return [dict(r) for r in self.s.q_all(sql, tuple(args))]

    def open_investigation(self, actor_id: str, key: str, reason: str,
                           evidence: str = "", op_id: str | None = None) -> dict:
        """安全员/库房可直接登记调查（标签破损通常由入库检查自动触发）。"""
        actor = self.require_person(actor_id)
        if "SAFETY" not in actor["roles"] and "KEEPER" not in actor["roles"]:
            raise DomainError("UNAUTHORIZED",
                              "只有安全员（SAFETY）或库房管理员（KEEPER）可登记调查", 403)
        if not reason:
            raise DomainError("BAD_REQUEST", "reason 必填", 400)
        payload = {"pod": key, "reason": reason, "evidence": evidence}

        def action():
            with self.s.tx() as c:
                pod = self._load_pod(c, key)
                if not pod:
                    raise DomainError("NOT_FOUND", f"版匣不存在：{key}", 404)
                existing = c.execute(
                    "SELECT * FROM investigation WHERE pod_id=? AND status='OPEN'",
                    (pod["id"],)).fetchone()
                if existing:
                    return {"investigation_id": existing["id"], "pod_id": pod["id"],
                            "status": "OPEN", "already_open": True}
                return self._open_investigation(c, pod, reason, evidence, actor["id"])
        return self.idempotent(op_id, "investigation.open", payload, action,
                               actor=actor["id"])

    def _open_investigation(self, c, pod, reason, evidence, opener_id,
                            handoff_id=None) -> dict:
        """版匣转入调查：冻结出库；先前保管关系仍保留可查。"""
        iid = "INV-" + secrets.token_hex(6)
        now = utcnow()
        c.execute(
            "INSERT INTO investigation(id,pod_id,inspection_id,reason,evidence,"
            "opened_by,opened_at,status) VALUES(?,?,NULL,?,?,?,?, 'OPEN')",
            (iid, pod["id"], reason, evidence, opener_id, now))
        c.execute("UPDATE custody_record SET ended_at=? WHERE pod_id=? AND ended_at IS NULL",
                  (now, pod["id"]))
        c.execute(
            "INSERT INTO custody_record(pod_id,holder_person,cabinet,handoff_id,reason,"
            "started_at) VALUES(?,?,'',?, 'INVESTIGATION',?)",
            (pod["id"], opener_id, handoff_id, now))
        c.execute(
            "UPDATE pod SET status='UNDER_INVESTIGATION',responsible_person=?,"
            "frozen_by_handoff=NULL WHERE id=?",
            (opener_id, pod["id"]))
        # 该版匣的未决交接因调查无法继续完成：保留申请，由确认侧裁决拒收
        self.s.audit(c, opener_id, "investigation.open", iid,
                     detail={"pod": pod["id"], "reason": reason})
        return {"investigation_id": iid, "pod_id": pod["id"], "status": "OPEN",
                "pod_status": "UNDER_INVESTIGATION"}

    def confirm_investigation_release(self, actor_id: str, investigation_id: str,
                                      role: str, new_holder: str,
                                      op_id: str | None = None) -> dict:
        """解除调查要求安全员（SAFETY）与工艺负责人（PROCESS）由不同自然人确认。"""
        role = (role or "").upper()
        if role not in ("SAFETY", "PROCESS"):
            raise DomainError("BAD_REQUEST", "role 必须是 SAFETY/PROCESS", 400)
        # 先看该人是否已以另一角色确认过：已决调查的"迟到确认"规则优先
        inv = self.s.q_one("SELECT * FROM investigation WHERE id=?",
                           (investigation_id,))
        if inv and inv["status"] != "OPEN":
            raise DomainError("INVESTIGATION_DECIDED",
                              f"调查已 {inv['status']}，迟到确认不得推翻", 409,
                              {"investigation_id": investigation_id,
                               "status": inv["status"]})
        prior = self.s.q_all(
            "SELECT role,person_id FROM investigation_confirm WHERE investigation_id=?",
            (investigation_id,))
        if any(p["person_id"] == actor_id for p in prior):
            raise DomainError("INVESTIGATION_SAME_PERSON",
                              "解除调查必须由安全员与工艺负责人两个不同自然人确认",
                              409, {"investigation_id": investigation_id})
        actor = self.require_role(actor_id, role)
        holder = self.require_person(new_holder)
        if "CUSTODIAN" not in holder["roles"]:
            raise DomainError("HANDOFF_PERSON_NO_DUTY",
                              f"解除后的责任人 {new_holder} 必须具备 CUSTODIAN 职责", 422)
        payload = {"investigation_id": investigation_id, "role": role,
                   "by": actor["id"], "new_holder": new_holder}

        def action():
            with self.s.tx() as c:
                inv = c.execute("SELECT * FROM investigation WHERE id=?",
                                (investigation_id,)).fetchone()
                if not inv:
                    raise DomainError("NOT_FOUND", "调查不存在", 404)
                if inv["status"] != "OPEN":
                    raise DomainError("INVESTIGATION_DECIDED",
                                      f"调查已 {inv['status']}，迟到确认不得推翻", 409)
                cur = c.execute(
                    "SELECT role,person_id FROM investigation_confirm WHERE investigation_id=?",
                    (investigation_id,)).fetchall()
                # 并发兜底：同一自然人即使兼两职也不能完成双确认
                if any(p["person_id"] == actor["id"] for p in cur):
                    raise DomainError("INVESTIGATION_SAME_PERSON",
                                      "解除调查必须由安全员与工艺负责人两个不同自然人确认",
                                      409)
                if any(p["role"] == role for p in cur):
                    raise DomainError("INVESTIGATION_ROLE_FILLED",
                                      f"{role} 角色已由 "
                                      f"{next(p['person_id'] for p in cur if p['role']==role)} 确认",
                                      409)
                c.execute(
                    "INSERT INTO investigation_confirm(investigation_id,role,person_id,at)"
                    " VALUES(?,?,?,?)",
                    (investigation_id, role, actor["id"], utcnow()))
                self.s.audit(c, actor["id"], "investigation.confirm", investigation_id,
                             op_id=op_id, detail={"role": role})
                done = {p["role"]: p["person_id"] for p in cur}
                done[role] = actor["id"]
                if not ({"SAFETY", "PROCESS"} <= set(done)):
                    return {"investigation_id": investigation_id, "status": "OPEN",
                            "confirmations": [{"role": k, "person_id": v}
                                              for k, v in done.items()],
                            "requires": [r for r in ("SAFETY", "PROCESS")
                                         if r not in done]}
                # 双角色齐备且为不同自然人（上面去重已保证）→ 解除
                now = utcnow()
                c.execute(
                    "UPDATE investigation SET status='RELEASED',released_at=? WHERE id=?",
                    (now, investigation_id))
                pod = c.execute("SELECT * FROM pod WHERE id=?",
                                (inv["pod_id"],)).fetchone()
                c.execute(
                    "UPDATE pod SET status='IN_STORE',responsible_person=? WHERE id=?",
                    (new_holder, pod["id"]))
                c.execute(
                    "UPDATE custody_record SET ended_at=? WHERE pod_id=? AND ended_at IS NULL",
                    (now, pod["id"]))
                c.execute(
                    "INSERT INTO custody_record(pod_id,holder_person,cabinet,handoff_id,"
                    "reason,started_at) VALUES(?,?,?,NULL, 'INVESTIGATION_RELEASED',?)",
                    (pod["id"], new_holder, pod["cabinet"], now))
                self.s.audit(c, actor["id"], "investigation.release", investigation_id,
                             op_id=op_id, detail={"pod": pod["id"],
                                                  "new_holder": new_holder,
                                                  "confirmer": done["SAFETY"],
                                                  "process": done["PROCESS"]})
                return {"investigation_id": investigation_id, "status": "RELEASED",
                        "pod_id": pod["id"], "pod_status": "IN_STORE",
                        "new_holder": new_holder}
        return self.idempotent(op_id, "investigation.confirm", payload, action,
                               actor=actor["id"])

    def list_investigations(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM investigation"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        sql += " ORDER BY opened_at DESC"
        out = []
        for r in self.s.q_all(sql, args):
            d = dict(r)
            d["confirmations"] = [dict(x) for x in self.s.q_all(
                "SELECT role,person_id,at FROM investigation_confirm WHERE investigation_id=?",
                (r["id"],))]
            out.append(d)
        return out

    # ================================================================ 启动恢复
    def _recover_handoffs(self) -> list[str]:
        """服务重启后接续待办：把超过确认期限的 PENDING 交接置 EXPIRED 并清除失效冻结。"""
        expired_ids = []
        now = utcnow()
        pending = self.s.q_all(
            "SELECT id FROM handoff WHERE status='PENDING' AND deadline_ts<=?", (now,))
        for row in pending:
            with self.s.tx() as c:
                h = c.execute("SELECT * FROM handoff WHERE id=?", (row["id"],)).fetchone()
                if h and h["status"] == "PENDING" and h["deadline_ts"] <= now:
                    self._expire_handoff(c, h)
                    expired_ids.append(h["id"])
        return expired_ids
