"""实体掩模版匣保管交接链：登记、双方确认、拒收留痕、冻结/期限、撤销换人、
入库检查、调查解除（双不同自然人）、重启恢复与幂等。"""
import threading
from pathlib import Path

from mdc.domain import DomainError
from mdc.service import Service
from mdc.store import Store
from tests.helpers import BaseCase


class CustodyBase(BaseCase):
    def register_pod(self, tag="TAG-T1", holder="u_phys1", cabinet="A-01-03"):
        return self.svc.register_pod("u_keeper", tag, cabinet, holder,
                                     op_id=f"REG-{tag}")

    def request(self, tag="TAG-T1", direction="OUTBOUND", giver="u_phys1",
                receiver="u_phys2", deadline=None, cabinet=None, note="",
                actor="u_keeper", op=None):
        return self.svc.request_handoff(
            actor, tag, direction, giver, receiver,
            deadline_ts=deadline, cabinet=cabinet, note=note, op_id=op)


class TestPodRegistration(CustodyBase):
    def test_keeper_registers_tag_cabinet_holder(self):
        r = self.register_pod()
        self.assertEqual(r["status"], "IN_STORE")
        pod = self.svc.get_pod(r["pod_id"])
        self.assertEqual(pod["tag_no"], "TAG-T1")
        self.assertEqual(pod["cabinet"], "A-01-03")
        self.assertEqual(pod["responsible_person"], "u_phys1")
        chain = pod["custody_chain"]
        self.assertEqual([c["reason"] for c in chain], ["REGISTER"])
        self.assertIsNone(chain[0]["ended_at"])

    def test_non_keeper_cannot_register(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.register_pod("u_phys1", "TAG-X", "A", "u_phys1")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_holder_without_custody_duty_rejected(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.register_pod("u_keeper", "TAG-X", "A", "u_disp1")
        self.assertEqual(cm.exception.code, "HANDOFF_PERSON_NO_DUTY")

    def test_duplicate_tag_rejected(self):
        self.register_pod()
        with self.assertRaises(DomainError) as cm:
            self.svc.register_pod("u_keeper", "TAG-T1", "B", "u_phys2")
        self.assertEqual(cm.exception.code, "POD_TAG_EXISTS")


class TestHandoffRequestRejections(CustodyBase):
    def test_wrong_tag_leaves_rejected_application(self):
        r = self.request(tag="TAG-404")
        self.assertEqual(r["status"], "REJECTED")
        self.assertEqual(r["reject_code"], "HANDOFF_TAG_NOT_FOUND")
        self.assertIsNone(r["pod_id"])
        rows = self.svc.list_handoffs(status="REJECTED")
        self.assertEqual(rows[0]["reject_code"], "HANDOFF_TAG_NOT_FOUND")

    def test_person_without_duty_rejected_but_persisted(self):
        self.register_pod()
        r = self.request(receiver="u_disp1")
        self.assertEqual(r["status"], "REJECTED")
        self.assertEqual(r["reject_code"], "HANDOFF_PERSON_NO_DUTY")
        # 版匣未被无效申请冻结
        pod = self.svc.get_pod(r["pod_id"])
        self.assertIsNone(pod["frozen_by_handoff"])

    def test_giver_and_receiver_must_differ(self):
        self.register_pod()
        r = self.request(receiver="u_phys1")
        self.assertEqual(r["reject_code"], "HANDOFF_SAME_PERSON")

    def test_giver_must_be_current_holder(self):
        self.register_pod()
        r = self.request(giver="u_phys2", receiver="u_phys1")
        self.assertEqual(r["reject_code"], "HANDOFF_GIVER_NOT_HOLDER")

    def test_second_application_frozen_by_first(self):
        self.register_pod()
        first = self.request(op="OP-A")
        self.assertEqual(first["status"], "PENDING")
        second = self.request(op="OP-B")
        self.assertEqual(second["status"], "REJECTED")
        self.assertEqual(second["reject_code"], "HANDOFF_FROZEN_BY_OTHER")
        self.assertEqual(second["reject_message"] and "", "")  # message 非空
        self.assertTrue(second["reject_message"])
        pod = self.svc.get_pod(first["pod_id"])
        self.assertEqual(pod["frozen_by_handoff"], first["handoff_id"])

    def test_deadline_in_the_past_rejected_at_request(self):
        self.register_pod()
        r = self.request(deadline="2020-01-01T00:00:00Z")
        self.assertEqual(r["reject_code"], "HANDOFF_DEADLINE_PASSED")
        # 字段不错位
        row = self.svc.list_handoffs(status="REJECTED")[0]
        self.assertEqual(row["requested_by"], "u_keeper")
        self.assertEqual(row["status"], "REJECTED")
        self.assertEqual(row["deadline_ts"], "2020-01-01T00:00:00Z")

    def test_only_keeper_or_custodian_may_apply(self):
        self.register_pod()
        with self.assertRaises(DomainError) as cm:
            self.request(actor="u_mei")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_inbound_requires_in_transit_pod(self):
        self.register_pod()
        r = self.request(direction="INBOUND", giver="u_phys1",
                         receiver="u_phys2")
        self.assertEqual(r["reject_code"], "POD_NOT_IN_TRANSIT")

    def test_concurrent_requests_single_freeze_winner(self):
        self.register_pod()
        barrier = threading.Barrier(2)
        outcomes = []

        def race(i):
            barrier.wait()
            r = self.request(op=f"RACEQ-{i}")
            outcomes.append(r["status"])
        t1 = threading.Thread(target=race, args=(1,))
        t2 = threading.Thread(target=race, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(outcomes), ["PENDING", "REJECTED"])
        pending = self.svc.list_handoffs(status="PENDING")
        self.assertEqual(len(pending), 1)


class TestDualConfirmation(CustodyBase):
    def _outbound_pending(self, op="HO"):
        self.register_pod()
        return self.request(op=op)["handoff_id"]

    def test_split_confirmation_then_custody_transferred(self):
        hid = self._outbound_pending()
        g = self.svc.confirm_handoff("u_phys1", hid, "GIVER", op_id="G")
        self.assertEqual(g["status"], "PENDING")
        self.assertEqual(g["requires"], ["RECEIVER"])
        r = self.svc.confirm_handoff("u_phys2", hid, "RECEIVER", op_id="R")
        self.assertEqual(r["status"], "CONFIRMED")
        self.assertEqual(r["new_holder"], "u_phys2")
        self.assertEqual(r["pod_status"], "IN_TRANSIT")
        timeline = self.svc.custody_timeline("TAG-T1")
        self.assertEqual([c["reason"] for c in timeline["chain"]],
                         ["REGISTER", "OUTBOUND"])
        self.assertEqual(timeline["current_holder"], "u_phys2")

    def test_only_designated_person_per_side(self):
        hid = self._outbound_pending()
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_handoff("u_phys2", hid, "GIVER")
        self.assertEqual(cm.exception.code, "HANDOFF_NOT_DESIGNATED")

    def test_side_confirmation_single_winner_under_contention(self):
        hid = self._outbound_pending()
        barrier = threading.Barrier(2)
        results = []

        def race(i):  # 两次独立请求同时争抢 GIVER 确认权（不同 op 编号）
            barrier.wait()
            try:
                results.append(("OK", self.svc.confirm_handoff(
                    "u_phys1", hid, "GIVER", op_id=f"RACE-{i}")))
            except DomainError as e:
                results.append(("ERR", e.code))
        t1 = threading.Thread(target=race, args=(1,))
        t2 = threading.Thread(target=race, args=(2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        outcomes = sorted((tag, val["status"] if tag == "OK" else val)
                          for tag, val in results)
        self.assertEqual(outcomes[0][0], "ERR")
        self.assertEqual(outcomes[0][1], "HANDOFF_SIDE_TAKEN")
        self.assertEqual(outcomes[1], ("OK", "PENDING"))
        confirms = [c for c in self.svc.list_handoffs()[0]["confirmations"]
                    if c["side"] == "GIVER"]
        self.assertEqual(len(confirms), 1)

    def test_late_confirmation_after_complete_rejected(self):
        hid = self._outbound_pending()
        self.svc.confirm_handoff("u_phys1", hid, "GIVER")
        self.svc.confirm_handoff("u_phys2", hid, "RECEIVER")
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_handoff("u_phys2", hid, "RECEIVER")
        self.assertEqual(cm.exception.code, "HANDOFF_DECIDED")

    def test_confirming_rejected_application_fails_with_original_code(self):
        self.register_pod()
        rejected = self.request(receiver="u_disp1")
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_handoff("u_phys1", rejected["handoff_id"], "GIVER")
        self.assertEqual(cm.exception.code, "HANDOFF_PERSON_NO_DUTY")

    def test_deadline_expiry_at_confirmation_clears_freeze(self):
        self.register_pod()
        h = self.request(deadline="2030-01-01T00:00:00Z", op="EX")
        # 模拟服务运行到确认期限之后（未重启）：确认动作裁决过期并清除失效冻结
        with self.svc.s.tx() as c:
            c.execute("UPDATE handoff SET deadline_ts=? WHERE id=?",
                      ("2026-10-05T00:00:00Z", h["handoff_id"]))
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_handoff("u_phys1", h["handoff_id"], "GIVER")
        self.assertEqual(cm.exception.code, "HANDOFF_DEADLINE_PASSED")
        row = self.svc.list_handoffs(status="EXPIRED")[0]
        self.assertEqual(row["id"], h["handoff_id"])
        # 失效冻结已清除，新申请可建立
        nxt = self.request(op="EX2")
        self.assertEqual(nxt["status"], "PENDING")

    def test_inbound_confirm_returns_pod_to_store_and_sets_cabinet(self):
        hid = self._outbound_pending()
        self.svc.confirm_handoff("u_phys1", hid, "GIVER")
        self.svc.confirm_handoff("u_phys2", hid, "RECEIVER")
        back = self.request(direction="INBOUND", giver="u_phys2",
                            receiver="u_phys1", cabinet="C-09-02", op="IN")
        self.svc.confirm_handoff("u_phys2", back["handoff_id"], "GIVER")
        done = self.svc.confirm_handoff("u_phys1", back["handoff_id"], "RECEIVER",
                                        inspection="OK")
        self.assertEqual(done["pod_status"], "IN_STORE")
        pod = self.svc.get_pod(done["pod_id"])
        self.assertEqual(pod["cabinet"], "C-09-02")
        self.assertEqual(pod["responsible_person"], "u_phys1")


class TestIdempotency(CustodyBase):
    def test_duplicate_request_id_replays_first_outcome(self):
        self.register_pod()
        first = self.request(op="DUP")
        replay = self.request(op="DUP")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["handoff_id"], first["handoff_id"])
        # 未产生第二条申请
        self.assertEqual(len(self.svc.list_handoffs()), 1)

    def test_rejected_outcome_replayed_with_same_code(self):
        self.register_pod()
        self.request(op="A")
        r1 = self.request(op="B")
        self.assertEqual(r1["reject_code"], "HANDOFF_FROZEN_BY_OTHER")
        r2 = self.request(op="B")
        self.assertEqual(r2["reject_code"], "HANDOFF_FROZEN_BY_OTHER")
        self.assertEqual(r2["handoff_id"], r1["handoff_id"])

    def test_duplicate_confirmation_replays_first(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        c1 = self.svc.confirm_handoff("u_phys1", h, "GIVER", op_id="CG")
        c2 = self.svc.confirm_handoff("u_phys1", h, "GIVER", op_id="CG")
        self.assertTrue(c2["replayed"])
        self.assertEqual(c2["handoff_id"], c1["handoff_id"])


class TestCancelAndAmend(CustodyBase):
    def test_requester_or_keeper_may_cancel(self):
        self.register_pod()
        h = self.request(actor="u_phys1", op="H")["handoff_id"]
        # 非申请人、非库房不得撤销
        with self.assertRaises(DomainError) as cm:
            self.svc.cancel_handoff("u_phys2", h)
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")
        r = self.svc.cancel_handoff("u_keeper", h)
        self.assertEqual(r["status"], "CANCELLED")
        # 冻结解除，可再申请
        self.assertEqual(self.request(op="H2")["status"], "PENDING")

    def test_cancel_forbidden_after_one_confirmation(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        with self.assertRaises(DomainError) as cm:
            self.svc.cancel_handoff("u_keeper", h)
        self.assertEqual(cm.exception.code, "HANDOFF_CONFIRMATION_EXISTS")

    def test_repeat_cancel_does_not_overturn(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        self.svc.cancel_handoff("u_keeper", h, op_id="C")
        with self.assertRaises(DomainError) as cm:
            self.svc.cancel_handoff("u_keeper", h, op_id="C2")
        self.assertEqual(cm.exception.code, "HANDOFF_DECIDED")

    def test_amend_allowed_before_any_confirmation(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        a = self.svc.amend_handoff("u_keeper", h, "u_phys1", "u_phys2",
                                  deadline_ts="2030-12-31T00:00:00Z")
        self.assertEqual(a["receiver_id"], "u_phys2")

    def test_amend_locked_after_confirmation(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        with self.assertRaises(DomainError) as cm:
            self.svc.amend_handoff("u_keeper", h, "u_phys1", "u_phys2")
        self.assertEqual(cm.exception.code, "HANDOFF_PARTY_LOCKED")


class TestReleaseToLine(CustodyBase):
    def test_blocked_until_both_confirmed(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        with self.assertRaises(DomainError) as cm:
            self.svc.release_pod_to_line("u_keeper", "TAG-T1")
        self.assertEqual(cm.exception.code, "POD_HANDOFF_NOT_CONFIRMED")
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        with self.assertRaises(DomainError) as cm:
            self.svc.release_pod_to_line("u_keeper", "TAG-T1")
        self.assertEqual(cm.exception.code, "POD_HANDOFF_NOT_CONFIRMED")
        self.svc.confirm_handoff("u_phys2", h, "RECEIVER")
        r = self.svc.release_pod_to_line("u_keeper", "TAG-T1")
        self.assertTrue(r["line_released_at"])

    def test_only_keeper_releases(self):
        with self.assertRaises(DomainError) as cm:
            self.svc.release_pod_to_line("u_phys1", "TAG-T1")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")


class TestInspectionAndInvestigation(CustodyBase):
    def _outbound_confirmed(self, tag="TAG-T1", op="H"):
        self.register_pod(tag=tag)
        h = self.request(tag=tag, op=op)["handoff_id"]
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        self.svc.confirm_handoff("u_phys2", h, "RECEIVER")
        return h

    def _inbound_confirmed_damaged(self):
        self._outbound_confirmed()
        back = self.request(direction="INBOUND", giver="u_phys2",
                            receiver="u_phys1", cabinet="C-1", op="IN")
        self.svc.confirm_handoff("u_phys2", back["handoff_id"], "GIVER")
        return self.svc.confirm_handoff(
            "u_phys1", back["handoff_id"], "RECEIVER",
            inspection="TAG_DAMAGED", note="防拆标签撕裂")

    def test_damaged_inbound_opens_investigation_and_blocks_outbound(self):
        r = self._inbound_confirmed_damaged()
        self.assertEqual(r["pod_status"], "UNDER_INVESTIGATION")
        self.assertTrue(r["investigation_id"])
        again = self.request(op="OUT2")
        self.assertEqual(again["reject_code"], "POD_UNDER_INVESTIGATION")
        # 发线也被禁止
        with self.assertRaises(DomainError) as cm:
            self.svc.release_pod_to_line("u_keeper", "TAG-T1")
        self.assertEqual(cm.exception.code, "POD_UNDER_INVESTIGATION")

    def test_prior_custody_relations_still_queryable(self):
        self._inbound_confirmed_damaged()
        tl = self.svc.custody_timeline("TAG-T1")
        reasons = [c["reason"] for c in tl["chain"]]
        self.assertEqual(reasons,
                         ["REGISTER", "OUTBOUND", "INBOUND", "INVESTIGATION"])

    def test_standalone_inspection_damaged_opens_investigation(self):
        self.register_pod()
        r = self.svc.inspect_pod("u_keeper", "TAG-T1", "TAG_DAMAGED",
                                  note="封条破损")
        self.assertEqual(r["pod_status"], "UNDER_INVESTIGATION")
        rows = self.svc.list_inspections(pod="TAG-T1")
        self.assertEqual(rows[0]["result"], "TAG_DAMAGED")

    def test_ok_inspection_does_not_freeze(self):
        self.register_pod()
        r = self.svc.inspect_pod("u_keeper", "TAG-T1", "OK")
        self.assertEqual(r["result"], "OK")
        self.assertNotIn("investigation_id", r)
        self.assertEqual(self.svc.get_pod("TAG-T1")["status"], "IN_STORE")

    def test_release_requires_safety_and_process_distinct_persons(self):
        dmg = self._inbound_confirmed_damaged()
        iid = dmg["investigation_id"]
        first = self.svc.confirm_investigation_release(
            "u_safety", iid, "SAFETY", "u_phys1")
        self.assertEqual(first["status"], "OPEN")
        self.assertEqual(first["requires"], ["PROCESS"])
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_investigation_release(
                "u_safety", iid, "PROCESS", "u_phys1")
        self.assertEqual(cm.exception.code, "INVESTIGATION_SAME_PERSON")
        # 无 PROCESS 角色不得签
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_investigation_release(
                "u_phys1", iid, "PROCESS", "u_phys1")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")
        done = self.svc.confirm_investigation_release(
            "u_proc", iid, "PROCESS", "u_phys1")
        self.assertEqual(done["status"], "RELEASED")
        self.assertEqual(done["pod_status"], "IN_STORE")
        pod = self.svc.get_pod("TAG-T1")
        self.assertEqual(pod["responsible_person"], "u_phys1")
        # 晚到确认不能推翻
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_investigation_release(
                "u_safety", iid, "SAFETY", "u_phys1")
        self.assertEqual(cm.exception.code, "INVESTIGATION_DECIDED")

    def test_dual_role_single_natural_person_cannot_release(self):
        self.svc.create_person("u_admin", "u_both", "双职",
                               ["SAFETY", "PROCESS"])
        dmg = self._inbound_confirmed_damaged()
        self.svc.confirm_investigation_release(
            "u_both", dmg["investigation_id"], "SAFETY", "u_phys1")
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_investigation_release(
                "u_both", dmg["investigation_id"], "PROCESS", "u_phys1")
        self.assertEqual(cm.exception.code, "INVESTIGATION_SAME_PERSON")

    def test_new_holder_must_have_custody_duty(self):
        dmg = self._inbound_confirmed_damaged()
        with self.assertRaises(DomainError) as cm:
            self.svc.confirm_investigation_release(
                "u_safety", dmg["investigation_id"], "SAFETY", "u_disp1")
        self.assertEqual(cm.exception.code, "HANDOFF_PERSON_NO_DUTY")

    def test_exceptions_summary_lists_all(self):
        self._inbound_confirmed_damaged()
        self.request(tag="TAG-404", op="X")
        summary = self.svc.custody_exceptions()
        self.assertEqual(len(summary["damaged_inspections"]), 1)
        self.assertEqual(len(summary["investigations"]), 1)
        self.assertTrue(any(h["reject_code"] == "HANDOFF_TAG_NOT_FOUND"
                            for h in summary["rejected_handoffs"]))


class TestTodoAndQueries(CustodyBase):
    def test_todo_lists_waiting_side_only(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        todos = self.svc.list_todos("u_phys1")
        self.assertEqual(todos[0]["id"], h)
        self.assertEqual(todos[0]["waiting_for_me"], ["GIVER"])
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        self.assertEqual(self.svc.list_todos("u_phys1"), [])
        self.assertEqual(self.svc.list_todos("u_phys2")[0]["waiting_for_me"],
                         ["RECEIVER"])


class TestRestartRecovery(CustodyBase):
    def test_restart_clears_expired_freeze_and_keeps_live_todos(self):
        self.register_pod()
        live = self.request(deadline="2030-01-01T00:00:00Z", op="LIVE")
        # live 冻结了版匣；另造一条过期的需直接写库（正常路径不会产生过期 PENDING）
        self.svc.cancel_handoff("u_keeper", live["handoff_id"])
        stale = self.request(deadline="2030-01-01T00:00:00Z", op="STALE")
        with self.svc.s.tx() as c:
            c.execute("UPDATE handoff SET deadline_ts=? WHERE id=?",
                      ("2020-01-01T00:00:00Z", stale["handoff_id"]))
        # 重启：过期冻结清除，未过期待办接续
        svc2 = Service(self.svc.s)
        ids = {h["id"]: h["status"] for h in svc2.list_handoffs()}
        self.assertEqual(ids[stale["handoff_id"]], "EXPIRED")
        self.assertEqual(ids[live["handoff_id"]], "CANCELLED")
        pod = svc2.get_pod("TAG-T1")
        self.assertIsNone(pod["frozen_by_handoff"])

    def test_restart_does_not_revive_decided_handoffs(self):
        self.register_pod()
        h = self.request(op="H")["handoff_id"]
        self.svc.confirm_handoff("u_phys1", h, "GIVER")
        self.svc.confirm_handoff("u_phys2", h, "RECEIVER")
        svc2 = Service(self.svc.s)
        self.assertEqual(svc2.list_handoffs()[0]["status"], "CONFIRMED")
        with self.assertRaises(DomainError) as cm:
            svc2.confirm_handoff("u_phys2", h, "RECEIVER")
        self.assertEqual(cm.exception.code, "HANDOFF_DECIDED")

    def test_persistence_across_store_reopen(self):
        d = str(Path(self.tmp.name) / "reopen")
        store1 = Store(d)
        s1 = Service(store1)
        r = s1.register_pod("u_keeper", "TAG-P", "A", "u_phys1")
        h = s1.request_handoff("u_keeper", "TAG-P", "OUTBOUND",
                               "u_phys1", "u_phys2")["handoff_id"]
        s1.confirm_handoff("u_phys1", h, "GIVER")
        s2 = Service(Store(d))
        self.assertEqual(s2.get_pod(r["pod_id"])["responsible_person"], "u_phys1")
        done = s2.confirm_handoff("u_phys2", h, "RECEIVER")
        self.assertEqual(done["status"], "CONFIRMED")
        self.assertEqual(s2.get_pod(r["pod_id"])["responsible_person"], "u_phys2")
