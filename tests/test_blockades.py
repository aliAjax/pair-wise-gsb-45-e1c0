"""封锁登记联动与可恢复改期的集成测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, ValidationError
from src.rules import AUTO_RESCHEDULED, MANUAL_RESCHEDULED, RESCHEDULE_PENDING


def plan_data(eta=6, etd=18, berth="B12", channel="C-EAST"):
    return {
        'vessel': 'HaiYun', 'berth': berth, 'channel': channel,
        'vessel_length_m': 180, 'berth_length_m': 220,
        'draft_m': 10.2, 'berth_depth_m': 11.5,
        'eta_hour': eta, 'etd_hour': etd,
        'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': '',
    }


class BlockadeScheduleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.actor = Actor("controller", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, **kwargs):
        return self.service.create(self.actor, ref, plan_data(**kwargs))

    def _confirm(self, record):
        return self.service.act(self.actor, record["id"], record["version"], "confirm", {"pilot_id": "P-1"})

    def test_channel_is_required_on_create(self):
        data = plan_data()
        del data["channel"]
        with self.assertRaises(ValidationError):
            self.service.create(self.actor, "VOY-1", data)

    def test_channel_window_conflict_rejected(self):
        self._create("VOY-1", berth="B12", channel="C-EAST")
        with self.assertRaises(Conflict):
            self._create("VOY-2", berth="B99", channel="C-EAST")

    def test_register_channel_closure_moves_draft_and_confirmed_only(self):
        draft = self._create("VOY-1", channel="C-EAST")
        confirmed = self._confirm(self._create("VOY-2", berth="B13", channel="C-NORTH"))
        berthed = self._confirm(self._create("VOY-3", berth="B14", channel="C-SOUTH"))
        berthed = self.service.act(self.actor, berthed["id"], berthed["version"], "berth", {"actual_draft_m": 10.3})
        departed = self._confirm(self._create("VOY-4", berth="B15", channel="C-WEST"))
        departed = self.service.act(self.actor, departed["id"], departed["version"], "berth", {"actual_draft_m": 10.3})
        departed = self.service.act(self.actor, departed["id"], departed["version"], "depart", {"cargo_operation_complete": True})
        other_channel = self._create("VOY-5", berth="B16", channel="C-DEEP")
        result = self.service.register_blockade(
            self.actor, {"kind": "channel_closure", "target": "C-EAST", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(result["affected_count"], 1)
        item = result["items"][0]
        self.assertEqual(item["record_id"], draft["id"])
        self.assertEqual(item["state"], AUTO_RESCHEDULED)
        self.assertEqual(item["recovery_status"], "auto_rescheduled")
        self.assertEqual(item["current_window"], {"eta_hour": 20, "etd_hour": 32})
        self.assertTrue(item["reschedulable"])
        self.assertIn("航道封控", item["reschedule_reason"])

        moved = self.service.get_record(self.actor, draft["id"])
        self.assertEqual(moved["state"], AUTO_RESCHEDULED)
        self.assertEqual(moved["schedule_info"]["mode"], "auto")
        self.assertEqual(moved["schedule_info"]["prior_state"], "draft")
        self.assertEqual(moved["schedule_info"]["original_eta_hour"], 6)
        # 已靠泊/离泊、异航道计划保持原状态
        self.assertEqual(self.service.get_record(self.actor, berthed["id"])["state"], "berthed")
        self.assertEqual(self.service.get_record(self.actor, departed["id"])["state"], "departed")
        self.assertEqual(self.service.get_record(self.actor, other_channel["id"])["state"], "draft")

    def test_berth_outage_moves_same_berth_only(self):
        target = self._create("VOY-1", berth="B12", channel="C-EAST")
        other = self._create("VOY-2", berth="B13", channel="C-NORTH")
        result = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(result["affected_count"], 1)
        self.assertEqual(result["items"][0]["record_id"], target["id"])
        self.assertEqual(self.service.get_record(self.actor, other["id"])["state"], "draft")

    def test_confirmed_plans_also_enter_rescheduling(self):
        confirmed = self._confirm(self._create("VOY-1"))
        result = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(result["items"][0]["state"], AUTO_RESCHEDULED)
        moved = self.service.get_record(self.actor, confirmed["id"])
        self.assertEqual(moved["schedule_info"]["prior_state"], "confirmed")

    def test_blockade_shortening_reshifts_then_restores(self):
        plan = self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        blockade = reg["blockade"]
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["payload"]["eta_hour"], 20)

        shortened = self.service.change_blockade_window(
            self.actor, blockade["id"], {"start_hour": 8, "end_hour": 10}
        )
        self.assertEqual(shortened["items"][0]["recovery_status"], "auto_reshifted")
        shifted = self.service.get_record(self.actor, plan["id"])
        self.assertEqual((shifted["payload"]["eta_hour"], shifted["payload"]["etd_hour"]), (10, 22))

        moved_away = self.service.change_blockade_window(
            self.actor, blockade["id"], {"start_hour": 40, "end_hour": 50}
        )
        self.assertEqual(moved_away["items"][0]["recovery_status"], "restored")
        restored = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(restored["state"], "draft")
        self.assertIsNone(restored["schedule_info"])
        self.assertEqual((restored["payload"]["eta_hour"], restored["payload"]["etd_hour"]), (6, 18))

    def test_revoke_only_restores_auto_rescheduled(self):
        auto = self._create("VOY-1")
        manual = self._confirm(self._create("VOY-2", berth="B13", channel="C-NORTH"))
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        reg2 = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B13", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(self.service.get_record(self.actor, manual["id"])["state"], AUTO_RESCHEDULED)
        locked = self.service.reschedule_manual(
            self.actor, self.service.get_record(self.actor, manual["id"])["id"],
            self.service.get_record(self.actor, manual["id"])["version"],
            {"eta_hour": 30, "etd_hour": 42, "reason": "船东要求"},
        )
        self.assertEqual(locked["recovery_status"], "manual_locked")

        result = self.service.revoke_blockade(self.actor, reg2["blockade"]["id"])
        self.assertEqual(result["items"], [])
        still_manual = self.service.get_record(self.actor, manual["id"])
        self.assertEqual(still_manual["state"], MANUAL_RESCHEDULED)
        self.assertEqual(still_manual["payload"]["eta_hour"], 30)
        self.assertFalse(still_manual["schedule_info"]["reschedulable"])

        result2 = self.service.revoke_blockade(self.actor, reg["blockade"]["id"])
        self.assertEqual(result2["items"][0]["recovery_status"], "restored")
        self.assertEqual(self.service.get_record(self.actor, auto["id"])["state"], "draft")

    def test_pending_plan_waits_for_manual_reschedule(self):
        # 用连续封锁把可行窗口铺满，使自动避让找不到空档
        plan = self._create("VOY-1", eta=6, etd=18)
        for hour in range(20, 708, 12):
            self.service.register_blockade(
                self.actor,
                {"kind": "berth_outage", "target": "B12", "start_hour": hour, "end_hour": hour + 12},
            )
        result = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(result["items"][0]["state"], RESCHEDULE_PENDING)
        self.assertEqual(result["items"][0]["recovery_status"], "pending")
        pending = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(pending["state"], RESCHEDULE_PENDING)

        # 封锁撤销不会自动恢复待改期计划
        self.service.revoke_blockade(self.actor, result["blockade"]["id"])
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["state"], RESCHEDULE_PENDING)

        # 只能人工改期
        fixed = self.service.reschedule_manual(
            self.actor, plan["id"], pending["version"], {"eta_hour": 6, "etd_hour": 18, "reason": "人工重新排定"}
        )
        self.assertEqual(fixed["record"]["state"], MANUAL_RESCHEDULED)

    def test_manual_reschedule_rejects_conflicting_window(self):
        plan = self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        moved = self.service.get_record(self.actor, plan["id"])
        with self.assertRaises(Conflict):
            self.service.reschedule_manual(
                self.actor, plan["id"], moved["version"], {"eta_hour": 9, "etd_hour": 12, "reason": "硬塞"}
            )

    def test_timeline_records_every_scheduling_decision(self):
        plan = self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        self.service.change_blockade_window(self.actor, reg["blockade"]["id"], {"start_hour": 40, "end_hour": 50})
        actions = [event["action"] for event in self.service.timeline(self.actor, plan["id"])]
        self.assertEqual(
            actions, ["created", "schedule_auto_rescheduled", "schedule_restored"]
        )
        blockade_events = self.service.blockade_timeline(self.actor, reg["blockade"]["id"])
        self.assertEqual(blockade_events[0]["action"], "blockade_registered")
        # 封锁时间线与计划时间线展示同一事件
        self.assertEqual(
            [e["action"] for e in blockade_events],
            ["blockade_registered", "schedule_auto_rescheduled", "blockade_window_changed", "schedule_restored"],
        )

    def test_create_against_active_blockade_rejected(self):
        self.service.register_blockade(
            self.actor, {"kind": "channel_closure", "target": "C-EAST", "start_hour": 8, "end_hour": 20}
        )
        with self.assertRaises(Conflict):
            self._create("VOY-1")
        # 窗口避开封锁即可创建
        ok = self._create("VOY-2", eta=20, etd=30)
        self.assertEqual(ok["state"], "draft")

    def test_multiple_blockades_on_same_berth(self):
        # 泊位停用 + 航道封控叠加：自动避让须同时避开两个封锁
        plan = self._create("VOY-1")
        r1 = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["payload"]["eta_hour"], 20)
        r2 = self.service.register_blockade(
            self.actor, {"kind": "channel_closure", "target": "C-EAST", "start_hour": 20, "end_hour": 30}
        )
        moved = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(moved["state"], AUTO_RESCHEDULED)
        self.assertEqual((moved["payload"]["eta_hour"], moved["payload"]["etd_hour"]), (30, 42))
        self.assertEqual(
            sorted(moved["schedule_info"]["related_blockade_ids"]),
            sorted([r1["blockade"]["id"], r2["blockade"]["id"]]),
        )
        # 撤销其中一个：另一个仍在，原窗口仍冲突 -> 按剩余封锁回挪而非恢复
        self.service.revoke_blockade(self.actor, r2["blockade"]["id"])
        still = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(still["state"], AUTO_RESCHEDULED)
        self.assertEqual(still["payload"]["eta_hour"], 20)
        # 撤销剩余封锁 -> 恢复原窗口
        self.service.revoke_blockade(self.actor, r1["blockade"]["id"])
        restored = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(restored["state"], "draft")
        self.assertEqual((restored["payload"]["eta_hour"], restored["payload"]["etd_hour"]), (6, 18))

    def test_manual_rescheduled_hit_by_new_blockade_invalidates(self):
        plan = self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        moved = self.service.get_record(self.actor, plan["id"])
        self.service.reschedule_manual(
            self.actor, plan["id"], moved["version"], {"eta_hour": 30, "etd_hour": 42, "reason": "人工"}
        )
        # 新封锁撞上人工改期窗口：失效待改期，但撤销时不会自动回退
        second = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 28, "end_hour": 40}
        )
        self.assertEqual(second["items"][0]["recovery_status"], "manual_invalidated")
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["state"], RESCHEDULE_PENDING)
        self.service.revoke_blockade(self.actor, second["blockade"]["id"])
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["state"], RESCHEDULE_PENDING)
