import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CONTROLLER = Actor("dispatcher", "port_controller")


def make_plan(berth="B12", channel="EAST-1", eta=6, etd=12, vessel="HaiYun"):
    return {
        "vessel": vessel, "berth": berth, "channel": channel,
        "vessel_length_m": 180, "berth_length_m": 220,
        "draft_m": 10.2, "berth_depth_m": 11.5,
        "eta_hour": eta, "etd_hour": etd,
        "risk_level": "medium", "dangerous_goods": False, "dangerous_class": "",
    }


def make_closure(kind="berth_outage", target="B12", start=8, end=20, reason="泊位检修"):
    return {"kind": kind, "target": target, "start_hour": start, "end_hour": end, "reason": reason}


class ClosureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, reference, **kwargs):
        return self.service.create(CONTROLLER, reference, make_plan(**kwargs))

    def _advance(self, record, steps):
        data = {"confirm": {"pilot_id": "P-01"}, "berth": {"actual_draft_m": 10.3}, "depart": {"cargo_operation_complete": True}}
        for action in steps:
            record = self.service.act(CONTROLLER, record["id"], record["version"], action, data[action])
        return record

    def _get(self, record_id):
        return self.service.get_record(CONTROLLER, record_id)

    def test_register_berth_outage_moves_only_draft_and_confirmed(self):
        v1 = self._create("VOY-1", eta=6, etd=12)
        v2 = self._advance(self._create("VOY-2", eta=12, etd=18), ["confirm"])
        v3 = self._advance(self._create("VOY-3", eta=18, etd=24), ["confirm", "berth"])
        v4 = self._create("VOY-4", berth="B05", eta=6, etd=12)
        v5 = self._advance(self._create("VOY-5", eta=0, etd=6), ["confirm", "berth", "depart"])
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=4, end=20))
        self.assertEqual(result["closure"]["status"], "active")
        self.assertEqual(result["affected_count"], 2)
        self.assertEqual({item["record_id"] for item in result["affected"]}, {v1["id"], v2["id"]})
        for item in result["affected"]:
            self.assertEqual(item["recovery"], "pending")
            self.assertEqual(item["to_state"], "reschedule_pending")
            self.assertIn("泊位B12停用", item["reason"])
        self.assertEqual(self._get(v1["id"])["payload"]["reschedule"]["previous_state"], "draft")
        self.assertEqual(self._get(v2["id"])["payload"]["reschedule"]["previous_state"], "confirmed")
        # 已靠泊、已离泊、其他泊位的计划保持原状态
        self.assertEqual(self._get(v3["id"])["state"], "berthed")
        self.assertEqual(self._get(v4["id"])["state"], "draft")
        self.assertEqual(self._get(v5["id"])["state"], "departed")
        # 列表、统计、时间线按同一结果展示
        self.assertEqual(self.service.stats(CONTROLLER)["reschedule_pending"], 2)
        self.assertEqual(len(self.service.list_records(CONTROLLER, state="reschedule_pending")), 2)
        timeline = self.service.timeline(CONTROLLER, v1["id"])
        self.assertEqual(timeline[-1]["action"], "closure_pending")
        self.assertEqual(timeline[-1]["details"]["closure_reference"], "CLO-1")

    def test_channel_closure_matches_by_channel(self):
        v1 = self._create("VOY-1", berth="B12", channel="EAST-1", eta=6, etd=12)
        v2 = self._create("VOY-2", berth="B05", channel="EAST-1", eta=6, etd=12)
        v3 = self._create("VOY-3", berth="B07", channel="WEST-9", eta=6, etd=12)
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(kind="channel_closure", target="EAST-1", reason="航道封控"))
        self.assertEqual({item["record_id"] for item in result["affected"]}, {v1["id"], v2["id"]})
        self.assertIn("航道EAST-1封控", result["affected"][0]["reason"])
        self.assertEqual(self._get(v3["id"])["state"], "draft")

    def test_adjust_and_revoke_restore_only_auto_plans(self):
        v1 = self._create("VOY-1", eta=6, etd=12)
        v2 = self._advance(self._create("VOY-2", eta=12, etd=18), ["confirm"])
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=8, end=20))
        closure = result["closure"]
        # 人工改期 v1 到封锁之外，标记为 manual
        pending_v1 = self._get(v1["id"])
        moved = self.service.act(CONTROLLER, v1["id"], pending_v1["version"], "reschedule", {"eta_hour": 20, "etd_hour": 22})
        self.assertEqual(moved["state"], "draft")
        self.assertEqual(moved["payload"]["reschedule"]["mode"], "manual")
        # 封锁改短到 8-12：v2(12-18) 不再重叠，自动恢复为 confirmed
        adjusted = self.service.adjust_closure(CONTROLLER, closure["id"], closure["version"], {"start_hour": 8, "end_hour": 12})
        self.assertEqual(adjusted["affected_count"], 1)
        item = adjusted["affected"][0]
        self.assertEqual(item["record_id"], v2["id"])
        self.assertEqual(item["recovery"], "restored")
        self.assertEqual(item["to_state"], "confirmed")
        self.assertEqual(self._get(v2["id"])["state"], "confirmed")
        # 人工改期过的 v1 不跟着回退
        self.assertEqual(self._get(v1["id"])["state"], "draft")
        self.assertEqual(self._get(v1["id"])["payload"]["eta_hour"], 20)
        # 撤销后没有仍需恢复的计划；人工改期的保持不动
        closure = adjusted["closure"]
        revoked = self.service.revoke_closure(CONTROLLER, closure["id"], closure["version"])
        self.assertEqual(revoked["closure"]["status"], "revoked")
        self.assertEqual(revoked["affected_count"], 0)
        self.assertEqual(self._get(v1["id"])["payload"]["eta_hour"], 20)
        # 封锁详情按同一结果展示每个计划的改期原因与恢复状态
        detail = self.service.get_closure(CONTROLLER, closure["id"])
        self.assertEqual(detail["affected_count"], 2)
        recovery = {item["record_id"]: item["recovery"] for item in detail["affected"]}
        self.assertEqual(recovery[v1["id"]], "manual")
        self.assertEqual(recovery[v2["id"]], "restored")
        timeline = self.service.closure_timeline(CONTROLLER, closure["id"])
        self.assertEqual([event["action"] for event in timeline], ["registered", "adjusted", "revoked"])

    def test_adjust_wider_invalidates_more_plans(self):
        v1 = self._create("VOY-1", eta=6, etd=12)
        v2 = self._create("VOY-2", eta=12, etd=18)
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=8, end=12))
        self.assertEqual(result["affected_count"], 1)
        closure = result["closure"]
        widened = self.service.adjust_closure(CONTROLLER, closure["id"], closure["version"], {"start_hour": 8, "end_hour": 20})
        self.assertEqual(widened["affected_count"], 1)
        self.assertEqual(widened["affected"][0]["record_id"], v2["id"])
        self.assertEqual(widened["affected"][0]["recovery"], "pending")
        self.assertEqual(self._get(v1["id"])["state"], "reschedule_pending")
        self.assertEqual(self._get(v2["id"])["state"], "reschedule_pending")

    def test_create_and_reschedule_must_avoid_closures(self):
        self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=8, end=20))
        with self.assertRaises(Conflict):
            self._create("VOY-1", eta=10, etd=12)
        ok = self._create("VOY-2", eta=20, etd=22)
        self.assertEqual(ok["state"], "draft")
        # 航道封控按航道拦截，不限泊位
        self.service.register_closure(CONTROLLER, "CLO-2", make_closure(kind="channel_closure", target="EAST-1", start=8, end=20, reason="航道封控"))
        with self.assertRaises(Conflict):
            self._create("VOY-3", berth="B07", eta=10, etd=12)

    def test_reschedule_must_avoid_closures_and_pending_only(self):
        v1 = self._create("VOY-1", eta=10, etd=12)
        self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=8, end=20))
        pending = self._get(v1["id"])
        self.assertEqual(pending["state"], "reschedule_pending")
        # 改到封锁时段内被拒绝
        with self.assertRaises(Conflict):
            self.service.act(CONTROLLER, v1["id"], pending["version"], "reschedule", {"eta_hour": 10, "etd_hour": 12})
        # 非法窗口被拒绝
        with self.assertRaises(ValidationError):
            self.service.act(CONTROLLER, v1["id"], pending["version"], "reschedule", {"eta_hour": 22, "etd_hour": 21})
        moved = self.service.act(CONTROLLER, v1["id"], pending["version"], "reschedule", {"eta_hour": 20, "etd_hour": 22})
        self.assertEqual(moved["state"], "draft")
        # 非待改期状态不允许改期
        with self.assertRaises(Conflict):
            self.service.act(CONTROLLER, v1["id"], moved["version"], "reschedule", {"eta_hour": 21, "etd_hour": 23})

    def test_failed_write_rolls_back_and_can_retry(self):
        v1 = self._create("VOY-1", eta=6, etd=12)
        repository = self.service.repository
        original = repository._apply_plan_updates

        def boom(connection, updates, now):
            raise sqlite3.OperationalError("simulated write failure")

        repository._apply_plan_updates = boom
        try:
            with self.assertRaises(sqlite3.OperationalError):
                self.service.register_closure(CONTROLLER, "CLO-1", make_closure())
        finally:
            repository._apply_plan_updates = original
        # 不留半套结果：封锁不存在、计划未变、审计无新事件
        self.assertEqual(self.service.list_closures(CONTROLLER), [])
        self.assertEqual(self._get(v1["id"])["state"], "draft")
        timeline = self.service.timeline(CONTROLLER, v1["id"])
        self.assertEqual([event["action"] for event in timeline], ["created"])
        # 重试成功；相同 reference 的重复提交被唯一约束拦截
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure())
        self.assertEqual(result["affected_count"], 1)
        with self.assertRaises(Conflict):
            self.service.register_closure(CONTROLLER, "CLO-1", make_closure())

    def test_stale_closure_version_rejected_without_side_effects(self):
        v1 = self._create("VOY-1", eta=6, etd=12)
        result = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=8, end=20))
        closure = result["closure"]
        with self.assertRaises(Conflict):
            self.service.adjust_closure(CONTROLLER, closure["id"], closure["version"] + 1, {"start_hour": 8, "end_hour": 10})
        fresh = self.service.get_closure(CONTROLLER, closure["id"])["closure"]
        self.assertEqual(fresh["end_hour"], 20)
        self.assertEqual(self._get(v1["id"])["state"], "reschedule_pending")
        with self.assertRaises(Conflict):
            self.service.revoke_closure(CONTROLLER, closure["id"], closure["version"] + 1)
        self.assertEqual(self.service.get_closure(CONTROLLER, closure["id"])["closure"]["status"], "active")

    def test_restore_skipped_when_another_closure_still_blocks(self):
        v1 = self._create("VOY-1", berth="B12", channel="EAST-1", eta=6, etd=12)
        first = self.service.register_closure(CONTROLLER, "CLO-1", make_closure(kind="berth_outage", target="B12", start=8, end=20))
        second = self.service.register_closure(CONTROLLER, "CLO-2", make_closure(kind="channel_closure", target="EAST-1", start=8, end=20, reason="航道封控"))
        # 撤销泊位停用，但航道封控仍覆盖该计划，不恢复
        revoked = self.service.revoke_closure(CONTROLLER, first["closure"]["id"], first["closure"]["version"])
        self.assertEqual(revoked["affected_count"], 0)
        self.assertEqual(self._get(v1["id"])["state"], "reschedule_pending")
        # 航道封控也撤销后自动恢复
        restored = self.service.revoke_closure(CONTROLLER, second["closure"]["id"], second["closure"]["version"])
        self.assertEqual(restored["affected_count"], 1)
        self.assertEqual(self._get(v1["id"])["state"], "draft")

    def test_closure_permission_and_validation(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_closure(Actor("outsider", "outsider"), "CLO-1", make_closure())
        with self.assertRaises(ValidationError):
            self.service.register_closure(CONTROLLER, "CLO-1", make_closure(start=20, end=8))
        with self.assertRaises(ValidationError):
            self.service.register_closure(CONTROLLER, "CLO-1", make_closure(kind="other"))
