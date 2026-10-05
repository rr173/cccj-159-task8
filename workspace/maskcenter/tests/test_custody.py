"""实体掩模版匣保管交接链。"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from mdc.domain import DomainError
from mdc.service import Service
from mdc.store import Store
from tests.helpers import BaseCase


class CustodyCase(BaseCase):
    def setUp(self):
        super().setUp()
        self.s = self.valid_set("CUST")
        self.dno = self.s["delivery_no"]
        self.pod = self.svc.custody.register_pod(
            "u_wh1", self.dno, "TAG-1", "A-01")["pod_id"]

    def request_out(self, receiver="u_line1", op=None, ttl=300):
        op = op or f"TR-OUT-{id(self)}"
        return self.svc.custody.request_transfer(
            "u_wh1", self.pod, "OUTBOUND", "TAG-1", receiver,
            ttl_seconds=ttl, op_id=op)

    def complete_out(self, receiver="u_line1", rid=None):
        r = rid or self.request_out()["transfer_id"]
        self.svc.custody.confirm_transfer("u_wh1", r, "C-WH")
        self.svc.custody.confirm_transfer(receiver, r, "C-LN")
        return r

    def complete_in(self, receiver="u_wh1", op_prefix="IN"):
        r = self.svc.custody.request_transfer(
            "u_line1", self.pod, "INBOUND", "TAG-1", receiver,
            cabinet="A-02", op_id=f"{op_prefix}-REQ-{id(self)}")["transfer_id"]
        self.svc.custody.confirm_transfer("u_line1", r, f"{op_prefix}-C1")
        self.svc.custody.confirm_transfer(receiver, r, f"{op_prefix}-C2")
        return r

    def test_register_requires_label_cabinet_and_warehouse_duty(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.register_pod("u_line1", self.dno, "X", "A-02")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.register_pod("u_wh1", self.dno, "TAG-1", "A-02")
        self.assertEqual(cm.exception.code, "LABEL_ALREADY_EXISTS")

    def test_rejected_application_remains_with_explicit_reason(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "WRONG-TAG", "u_line1",
                ttl_seconds=300, op_id="BAD-LABEL")
        self.assertEqual(cm.exception.code, "LABEL_MISMATCH")
        rows = self.svc.custody.list_transfers("REJECTED")
        self.assertEqual(rows[0]["reject_reason"], "LABEL_MISMATCH")
        self.assertEqual(self.svc.custody.get_pod(self.pod)["status"], "IN_STORAGE")

    def test_person_without_required_duty_is_rejected_and_retained(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "TAG-1", "u_wh2",
                ttl_seconds=300, op_id="BAD-DUTY")
        self.assertEqual(cm.exception.code, "PERSON_ROLE_INVALID")
        self.assertTrue(self.svc.custody.list_transfers("REJECTED"))

    def test_duplicate_request_id_replays_first_result(self):
        a = self.request_out(op="DUP")
        b = self.request_out(op="DUP")
        self.assertTrue(b["replayed"])
        self.assertEqual(a["transfer_id"], b["transfer_id"])

    def test_creation_after_passed_deadline_is_retained(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "TAG-1", "u_line1",
                deadline_at="2000-01-01T00:00:00Z", op_id="OLD-DEADLINE")
        self.assertEqual(cm.exception.code, "CONFIRMATION_DEADLINE_PASSED")
        rejected = self.svc.custody.list_transfers("REJECTED")
        self.assertEqual(rejected[0]["reject_reason"], "CONFIRMATION_DEADLINE_PASSED")

    def test_unknown_pod_application_is_retained(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", "CTP-MISSING", "OUTBOUND", "TAG-X", "u_line1",
                ttl_seconds=300, op_id="MISSING-POD")
        self.assertEqual(cm.exception.code, "POD_NOT_FOUND")
        row = self.svc.s.q_one(
            "SELECT pod_id,reject_reason FROM custody_transfer WHERE reject_reason='POD_NOT_FOUND'")
        self.assertIsNone(row["pod_id"])

    def test_unknown_receiver_application_is_rejected_and_retained(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "TAG-1", "u_missing",
                ttl_seconds=300, op_id="BAD-PERSON")
        self.assertEqual(cm.exception.code, "PERSON_ROLE_INVALID")
        self.assertTrue(self.svc.custody.list_transfers("REJECTED"))

    def test_second_application_frozen_by_pending_transfer(self):
        first = self.request_out(op="FIRST")["transfer_id"]
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "TAG-1", "u_line2",
                ttl_seconds=300, op_id="SECOND")
        self.assertEqual(cm.exception.code, "POD_FROZEN")
        self.assertEqual(self.svc.custody.get_pod(self.pod)["active_transfer"]["id"],
                         first)

    def test_two_side_confirmation_completes_and_issues_to_line(self):
        rid = self.complete_out()
        pod = self.svc.custody.get_pod(self.pod)
        self.assertEqual(pod["status"], "ISSUED_TO_LINE")
        self.assertEqual(pod["owner_person_id"], "u_line1")
        self.assertIsNone(pod["active_transfer"])
        history = self.svc.custody.custody_history(self.pod)
        events = [x["event"] for x in history["relations"]]
        self.assertIn("OUTBOUND_COMPLETED", events)
        self.assertEqual(history["transfers"][0]["id"], rid)

    def test_first_eligible_receiver_wins_when_receiver_unspecified(self):
        req = self.svc.custody.request_transfer(
            "u_wh1", self.pod, "OUTBOUND", "TAG-1", None,
            ttl_seconds=300, op_id="RACE-REQ")
        rid = req["transfer_id"]
        self.svc.custody.confirm_transfer("u_wh1", rid, "RACE-WH")
        first = self.svc.custody.confirm_transfer("u_line1", rid, "RACE-L1")
        self.assertEqual(first["owner_person_id"], "u_line1")
        self.assertEqual(first["status"], "COMPLETED")
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.confirm_transfer("u_line2", rid, "RACE-L2")
        self.assertIn(cm.exception.code, {"RECEIVER_MISMATCH", "TRANSFER_DECIDED"})
        self.assertEqual(cm.exception.code, "RECEIVER_MISMATCH"
                         if self.svc.custody.list_todo("u_line2")["transfers"]
                         else "TRANSFER_DECIDED")
        t = self.svc.s.q_one("SELECT receiver_person FROM custody_transfer WHERE id=?",
                             (rid,))
        self.assertEqual(t["receiver_person"], "u_line1")
        self.assertEqual(self.svc.custody.get_pod(self.pod)["status"],
                         "ISSUED_TO_LINE")

    def test_concurrent_receiver_confirmation_has_single_winner(self):
        rid = self.svc.custody.request_transfer(
            "u_wh1", self.pod, "OUTBOUND", "TAG-1", None,
            ttl_seconds=300, op_id=f"RACE-REQ-{id(self)}")["transfer_id"]
        self.svc.custody.confirm_transfer("u_wh1", rid, f"RACE-WH-{id(self)}")
        results, errors = [], []

        def receiver(person: str, seq: int):
            try:
                r = self.svc.custody.confirm_transfer(
                    person, rid, f"RACE-{person}-{id(self)}-{seq}")
                results.append((person, r.get("status")))
            except DomainError as e:
                errors.append((person, e.code))

        threads = [threading.Thread(target=receiver, args=(p, i))
                   for i, p in enumerate(("u_line1", "u_line2"))]
        for x in threads:
            x.start()
        for x in threads:
            x.join()
        completed = [p for p, status in results if status == "COMPLETED"]
        rejected = [(p, code) for p, code in errors
                    if code in ("RECEIVER_MISMATCH", "TRANSFER_DECIDED")]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(self.svc.custody.get_pod(self.pod)["owner_person_id"],
                         completed[0])

    def test_receiver_cannot_be_replaced_after_first_confirmation(self):
        # 指定 u_line1 后，u_line2 永远不能抢占；即使接收侧尚未确认。
        rid = self.request_out(receiver="u_line1", op="FIXED")["transfer_id"]
        self.svc.custody.confirm_transfer("u_wh1", rid, "FIXED-WH")
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.confirm_transfer("u_line2", rid, "FIXED-L2")
        self.assertEqual(cm.exception.code, "RECEIVER_MISMATCH")

    def test_confirmation_after_deadline_cannot_complete(self):
        rid = self.request_out(ttl=60, op="SHORT")["transfer_id"]
        with self.svc.s.tx() as c:
            c.execute("UPDATE custody_transfer SET deadline_at='2000-01-01T00:00:00Z'"
                      " WHERE id=?", (rid,))
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.confirm_transfer("u_wh1", rid, "LATE")
        self.assertEqual(cm.exception.code, "CONFIRMATION_DEADLINE_PASSED")
        self.assertEqual(self.svc.custody.get_pod(self.pod)["status"], "IN_STORAGE")
        with self.assertRaises(DomainError) as cm2:
            self.svc.custody.confirm_transfer("u_wh1", rid, "LATE2")
        self.assertEqual(cm2.exception.code, "CONFIRMATION_DEADLINE_PASSED")

    def test_only_requester_or_warehouse_admin_can_cancel(self):
        rid = self.request_out(op="CANCEL-REQ")["transfer_id"]
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.cancel_transfer("u_line1", rid, "BAD-CANCEL")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")
        out = self.svc.custody.cancel_transfer("u_wh1", rid, "CANCEL")
        self.assertEqual(out["status"], "CANCELLED")
        self.assertEqual(self.svc.custody.get_pod(self.pod)["status"], "IN_STORAGE")
        # 同一撤销请求标识重放首次结果；换用新编号也不能推翻终态。
        replay = self.svc.custody.cancel_transfer("u_wh1", rid, "CANCEL")
        self.assertTrue(replay.get("replayed"))
        again = self.svc.custody.cancel_transfer(
            "u_wh1", rid, f"CANCEL-AGAIN-{id(self)}")
        self.assertTrue(again["noop"])
        self.assertEqual(again["status"], "CANCELLED")

    def test_exposure_gate_requires_completed_outbound(self):
        tool, lot = self.tool_and_lot(self.dno)
        rid = self.request_out(op="PEND-FOR-EXPOSE")["transfer_id"]
        w = self.svc.reserve_window(
            "u_disp1", tool, self.dno, self.s["order"][0],
            "2026-10-10T08:00:00Z", "2026-10-10T09:00:00Z", lot, op_id="WGATE")
        with self.assertRaises(DomainError) as cm:
            self.svc.expose("u_disp1", w["window_id"], "EX-BAD")
        self.assertEqual(cm.exception.code, "POD_NOT_ISSUED")
        self.svc.custody.cancel_transfer("u_wh1", rid, "CANCEL-PEND")
        rid2 = self.request_out(op="OUT-FOR-EXPOSE")["transfer_id"]
        self.svc.custody.confirm_transfer("u_wh1", rid2, "GATE-WH")
        self.svc.custody.confirm_transfer("u_line1", rid2, "GATE-LN")
        self.svc.expose("u_disp1", w["window_id"], "EX-OK")
        self.assertEqual(self.svc.list_windows()[0]["state"], "EXPOSED")

    def test_damaged_inbound_label_opens_investigation_and_blocks_outbound(self):
        self.complete_out()
        rid = self.complete_in()
        inspection = self.svc.custody.inspect_inbound(
            "u_wh1", self.pod, "LABEL_DAMAGED", note="tag cut", op_id="INS-DMG")
        inv = inspection["investigation_id"]
        self.assertEqual(self.svc.custody.get_pod(self.pod)["status"],
                         "INVESTIGATING")
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.request_transfer(
                "u_wh1", self.pod, "OUTBOUND", "TAG-1", "u_line1",
                ttl_seconds=300, op_id="OUT-DURING-INV")
        self.assertEqual(cm.exception.code, "POD_UNDER_INVESTIGATION")
        # 先前保管关系仍可查询，且包含入库交接。
        history = self.svc.custody.custody_history(self.pod)
        self.assertTrue(any(x["id"] == rid for x in history["transfers"]))
        self.assertTrue(history["investigations"])
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.inspect_inbound(
                "u_wh1", self.pod, "INTACT", op_id="INS-CONFLICT")
        self.assertEqual(cm.exception.code, "POD_NOT_AWAITING_INSPECTION")

    def test_investigation_release_requires_distinct_safety_and_process(self):
        self.complete_out()
        self.complete_in(op_prefix="IN2")
        inv = self.svc.custody.inspect_inbound(
            "u_wh1", self.pod, "LABEL_DAMAGED", op_id="INS2")["investigation_id"]
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.confirm_investigation("u_safe", inv, "REL-NO-LABEL")
        self.assertEqual(cm.exception.code, "LABEL_REPLACEMENT_REQUIRED")
        self.svc.custody.relabel_during_investigation(
            "u_wh1", self.pod, "TAG-2", op_id="RELABEL")
        self.svc.create_person("u_admin", "u_both", "安工双角色",
                               ["SAFETY", "PROCESS"])
        self.svc.custody.confirm_investigation("u_both", inv, "REL-BOTH-1")
        with self.assertRaises(DomainError) as cm:
            self.svc.custody.confirm_investigation("u_both", inv, "REL-BOTH-2")
        self.assertEqual(cm.exception.code, "INVESTIGATION_SAME_PERSON")
        out = self.svc.custody.confirm_investigation("u_proc", inv, "REL-PROC")
        self.assertEqual(out["status"], "RELEASED")
        pod = self.svc.custody.get_pod(self.pod)
        self.assertEqual(pod["status"], "IN_STORAGE")
        self.assertEqual(pod["label_no"], "TAG-2")

    def test_restart_expires_deadline_and_restores_custody_but_keeps_active_todo(self):
        root = Path(self.tmp.name)
        rid = self.request_out(ttl=3600, op="KEEP")["transfer_id"]
        restarted = Service(Store(root))  # 未过期：待办接续
        self.assertEqual(
            restarted.custody.get_pod(self.pod)["active_transfer"]["id"], rid)
        with Store(root).tx() as c:
            c.execute("UPDATE custody_transfer SET deadline_at='2000-01-01T00:00:00Z'"
                      " WHERE id=?", (rid,))
        svc2 = Service(Store(root))
        # 构造时已执行一次恢复；显式再跑应得到幂等空结果，版匣仍保持解冻状态。
        result = svc2.custody.recover_on_startup()
        self.assertEqual(svc2.s.q_one(
            "SELECT status FROM custody_transfer WHERE id=?", (rid,))["status"],
            "EXPIRED")
        self.assertEqual(result["expired_transfers"], [])
        self.assertEqual(Store(root).q_one(
            "SELECT status FROM custody_pod WHERE id=?", (self.pod,))["status"],
            "IN_STORAGE")

    def test_todo_lists_waiting_party_and_board_exceptions(self):
        rid = self.request_out(op="TODO")["transfer_id"]
        todo = self.svc.custody.list_todo("u_wh1")
        self.assertEqual(todo["transfers"][0]["id"], rid)
        self.assertEqual(todo["transfers"][0]["my_side"], "SURRENDER")
        self.svc.custody.confirm_transfer("u_wh1", rid, "TODO-WH")
        todo_line = self.svc.custody.list_todo("u_line1")
        self.assertEqual(todo_line["transfers"][0]["my_side"], "RECEIVER")
        exceptions = self.svc.custody.list_exceptions()
        self.assertIn("rejected_or_expired_transfers", exceptions)
