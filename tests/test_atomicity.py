"""事务原子性与幂等重试测试。"""
import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict
from src.repository import Repository
from src.rules import AUTO_RESCHEDULED


def plan_data(eta=6, etd=18, berth="B12", channel="C-EAST"):
    return {
        'vessel': 'HaiYun', 'berth': berth, 'channel': channel,
        'vessel_length_m': 180, 'berth_length_m': 220,
        'draft_m': 10.2, 'berth_depth_m': 11.5,
        'eta_hour': eta, 'etd_hour': etd,
        'risk_level': 'medium', 'dangerous_goods': False, 'dangerous_class': '',
    }


class AtomicityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db_path)
        self.actor = Actor("controller", "port_controller")

    def tearDown(self):
        self.temp.cleanup()

    def _create(self, ref, **kwargs):
        return self.service.create(self.actor, ref, plan_data(**kwargs))

    def test_failure_rolls_back_blockade_plans_and_audit_together(self):
        plan = self._create("VOY-1")
        original_state = self.service.get_record(self.actor, plan["id"])

        real_force_change = Repository.force_record_change
        calls = {"n": 0}

        def flaky_force_change(self, connection, record, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated write failure")
            return real_force_change(self, connection, record, *args, **kwargs)

        Repository.force_record_change = flaky_force_change
        try:
            with self.assertRaises(RuntimeError):
                self.service.register_blockade(
                    self.actor,
                    {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20},
                )
        finally:
            Repository.force_record_change = real_force_change

        # 计划未变
        after = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(after["state"], original_state["state"])
        self.assertEqual(after["version"], original_state["version"])
        self.assertEqual(after["payload"]["eta_hour"], 6)
        self.assertIsNone(after["schedule_info"])
        # 封锁未留下
        self.assertEqual(self.service.list_blockades(self.actor), [])
        # 审计无新增（计划与封锁都没有半套事件）
        self.assertEqual(
            [e["action"] for e in self.service.timeline(self.actor, plan["id"])],
            ["created"],
        )
        # 数据库仍然一致可用
        stats = self.service.stats(self.actor)
        self.assertEqual(stats["blockades"], {})

    def test_retry_same_idempotency_key_replays_result(self):
        plan = self._create("VOY-1")
        payload = {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}

        first = self.service.register_blockade(self.actor, dict(payload), idempotency_key="key-1")
        second = self.service.register_blockade(self.actor, dict(payload), idempotency_key="key-1")
        self.assertEqual(first["blockade"]["id"], second["blockade"]["id"])
        self.assertEqual(first["items"], second["items"])
        # 没有重复登记封锁
        self.assertEqual(len(self.service.list_blockades(self.actor)), 1)
        # 计划只被改期一次（版本只增加一次）
        record = self.service.get_record(self.actor, plan["id"])
        self.assertEqual(record["state"], AUTO_RESCHEDULED)
        self.assertEqual(record["version"], 2)
        self.assertEqual(
            [e["action"] for e in self.service.timeline(self.actor, plan["id"])],
            ["created", "schedule_auto_rescheduled"],
        )

    def test_idempotency_key_rejects_different_request(self):
        self._create("VOY-1")
        self.service.register_blockade(
            self.actor,
            {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20},
            idempotency_key="key-x",
        )
        with self.assertRaises(Conflict):
            self.service.register_blockade(
                self.actor,
                {"kind": "berth_outage", "target": "B12", "start_hour": 9, "end_hour": 20},
                idempotency_key="key-x",
            )

    def test_revoke_idempotent_retry(self):
        self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        first = self.service.revoke_blockade(self.actor, reg["blockade"]["id"], idempotency_key="rev-1")
        # 不带键的重复撤销直接报冲突
        with self.assertRaises(Conflict):
            self.service.revoke_blockade(self.actor, reg["blockade"]["id"])
        # 带同一键重试返回首次结果
        second = self.service.revoke_blockade(self.actor, reg["blockade"]["id"], idempotency_key="rev-1")
        self.assertEqual(first["items"], second["items"])
        self.assertEqual(
            self.service.list_blockades(self.actor, status="revoked")[0]["version"], 2
        )

    def test_manual_reschedule_idempotent_retry(self):
        plan = self._create("VOY-1")
        reg = self.service.register_blockade(
            self.actor, {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20}
        )
        moved = self.service.get_record(self.actor, plan["id"])
        data = {"eta_hour": 30, "etd_hour": 42, "reason": "人工"}
        first = self.service.reschedule_manual(
            self.actor, plan["id"], moved["version"], data, idempotency_key="man-1"
        )
        second = self.service.reschedule_manual(
            self.actor, plan["id"], moved["version"], data, idempotency_key="man-1"
        )
        self.assertEqual(first["record"]["version"], second["record"]["version"])
        self.assertEqual(
            [e["action"] for e in self.service.timeline(self.actor, plan["id"])].count("reschedule"),
            1,
        )

    def test_write_failure_then_successful_retry(self):
        plan = self._create("VOY-1")
        real_create_blockade = Repository.create_blockade
        calls = {"n": 0}

        def flaky_create(self, connection, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("disk full")
            return real_create_blockade(self, connection, *args, **kwargs)

        Repository.create_blockade = flaky_create
        try:
            with self.assertRaises(RuntimeError):
                self.service.register_blockade(
                    self.actor,
                    {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20},
                    idempotency_key="retry-1",
                )
        finally:
            Repository.create_blockade = real_create_blockade

        # 第一次整体回滚，幂等表也未留记录，重试可正常成功
        result = self.service.register_blockade(
            self.actor,
            {"kind": "berth_outage", "target": "B12", "start_hour": 8, "end_hour": 20},
            idempotency_key="retry-1",
        )
        self.assertEqual(result["affected_count"], 1)
        self.assertEqual(self.service.get_record(self.actor, plan["id"])["state"], AUTO_RESCHEDULED)
