"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text
from .repository import Repository
from .rules import PENDING_STATE, RESTORABLE_STATES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        self._ensure_no_closure_conflict(prepared)
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        if action == "reschedule":
            others = [item for item in self.repository.list_records(limit=500) if item["id"] != record_id]
            self.rules.check_create_conflicts(new_payload, others)
            self._ensure_no_closure_conflict(new_payload)
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 泊位停用/航道封控：登记、改时段、撤销与计划联动 ----

    def _ensure_closure_action_role(self, actor: Actor, action: str) -> None:
        if not self.rules.role_can_closure_action(actor.role, action):
            raise PermissionDenied("角色无权执行该封锁操作")

    def _ensure_no_closure_conflict(self, payload: Dict[str, Any]) -> None:
        for closure in self.repository.list_closures(status="active", limit=500):
            if self.rules.closure_overlap(closure, payload):
                raise Conflict("计划窗口撞上封锁：%s" % self.rules.closure_reason(closure))

    def _pending_candidates(self, closure: Dict[str, Any]) -> List[Dict[str, Any]]:
        """同泊位/同航道且窗口撞上封锁的草稿、已确认计划；已靠泊、已离泊等保持原状态。"""
        return [
            record
            for record in self.repository.list_records(limit=500)
            if record["state"] in RESTORABLE_STATES and self.rules.closure_overlap(closure, record["payload"])
        ]

    def _auto_pending_records(self) -> List[Dict[str, Any]]:
        """全部因封锁自动进入待改期、且尚未人工改期的计划。"""
        result = []
        for record in self.repository.list_records(limit=500):
            if record["state"] != PENDING_STATE:
                continue
            marker = record["payload"].get("reschedule") or {}
            if marker.get("mode") == "auto":
                result.append(record)
        return result

    @staticmethod
    def _pending_update(record: Dict[str, Any], closure: Dict[str, Any], reason_text: str, actor: Actor) -> Dict[str, Any]:
        payload = dict(record["payload"])
        payload["reschedule"] = {
            "closure_reference": closure["reference"],
            "kind": closure["kind"],
            "target": closure["target"],
            "reason": reason_text,
            "mode": "auto",
            "previous_state": record["state"],
            "recovery": "pending",
        }
        return {
            "record_id": record["id"],
            "expected_version": record["version"],
            "state": PENDING_STATE,
            "payload": payload,
            "actor_id": actor.user_id,
            "action": "closure_pending",
            "details": {"summary": "封锁生效，计划失效进入待改期", "closure_reference": closure["reference"], "kind": closure["kind"], "target": closure["target"], "reason": reason_text, "from": record["state"], "to": PENDING_STATE},
            "view": {"reason": reason_text, "recovery": "pending"},
        }

    @staticmethod
    def _restore_update(record: Dict[str, Any], actor: Actor, reason_text: str) -> Dict[str, Any]:
        payload = dict(record["payload"])
        marker = dict(payload.get("reschedule") or {})
        previous = marker.get("previous_state") or "draft"
        marker["recovery"] = "restored"
        payload["reschedule"] = marker
        return {
            "record_id": record["id"],
            "expected_version": record["version"],
            "state": previous,
            "payload": payload,
            "actor_id": actor.user_id,
            "action": "closure_restored",
            "details": {"summary": reason_text, "closure_reference": marker.get("closure_reference"), "kind": marker.get("kind"), "target": marker.get("target"), "from": PENDING_STATE, "to": previous},
            "view": {"reason": reason_text, "recovery": "restored"},
        }

    @staticmethod
    def _views(updates: List[Dict[str, Any]], updated: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        views = []
        for update, record in zip(updates, updated):
            views.append({
                "record_id": record["id"],
                "reference": record["reference"],
                "vessel": record["payload"].get("vessel"),
                "from_state": update["details"]["from"],
                "to_state": record["state"],
                "reason": update["view"]["reason"],
                "recovery": update["view"]["recovery"],
            })
        return views

    def _restore_updates(self, actor: Actor, reason_text: str, exclude_closure_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """全局自愈扫描：自动改期且已无任何活跃封锁重叠的计划恢复原状态；人工改期的不回退。"""
        active = [item for item in self.repository.list_closures(status="active", limit=500) if item["id"] != exclude_closure_id]
        updates = []
        for record in self._auto_pending_records():
            if any(self.rules.closure_overlap(closure, record["payload"]) for closure in active):
                continue
            updates.append(self._restore_update(record, actor, reason_text))
        return updates

    def register_closure(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权登记封锁")
        reference = text({"reference": reference}, "reference")
        fields = self.rules.validate_closure(payload or {})
        fields["reference"] = reference
        reason_text = self.rules.closure_reason(fields)
        updates = [self._pending_update(record, fields, reason_text, actor) for record in self._pending_candidates(fields)]
        details = {
            "summary": "登记%s" % ("泊位停用" if fields["kind"] == "berth_outage" else "航道封控"),
            "kind": fields["kind"],
            "target": fields["target"],
            "start_hour": fields["start_hour"],
            "end_hour": fields["end_hour"],
            "reason": fields["reason"],
            "affected_count": len(updates),
            "plan_ids": [update["record_id"] for update in updates],
        }
        closure, updated = self.repository.create_closure_with_effects(fields, updates, "registered", details, actor.user_id)
        return {"closure": closure, "affected": self._views(updates, updated), "affected_count": len(updated)}

    def adjust_closure(self, actor: Actor, closure_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_closure_action_role(actor, "adjust")
        closure = self.repository.get_closure(closure_id)
        if closure["status"] != "active":
            raise Conflict("封锁已撤销，无法调整时段")
        changes = self.rules.validate_closure_adjust(data or {}, closure)
        adjusted = dict(closure)
        adjusted.update(changes)
        reason_text = self.rules.closure_reason(adjusted)
        # 时段一变：新撞上的草稿/已确认计划先失效并待改期
        updates = [self._pending_update(record, adjusted, reason_text, actor) for record in self._pending_candidates(adjusted)]
        # 改短后不再被任何活跃封锁覆盖的自动改期计划恢复原状态（按调整后窗口评估）
        remaining = [item for item in self.repository.list_closures(status="active", limit=500) if item["id"] != closure_id]
        remaining.append(adjusted)
        for record in self._auto_pending_records():
            if any(self.rules.closure_overlap(blocker, record["payload"]) for blocker in remaining):
                continue
            updates.append(self._restore_update(record, actor, "封锁时段调整，自动恢复原状态"))
        details = {
            "summary": "封锁时段调整",
            "kind": closure["kind"],
            "target": closure["target"],
            "old_window": {"start_hour": closure["start_hour"], "end_hour": closure["end_hour"]},
            "new_window": {"start_hour": adjusted["start_hour"], "end_hour": adjusted["end_hour"]},
            "affected_count": len(updates),
            "plan_ids": [update["record_id"] for update in updates],
        }
        closure_row, updated = self.repository.update_closure_with_effects(closure_id, int(expected_version), changes, updates, "adjusted", details, actor.user_id)
        return {"closure": closure_row, "affected": self._views(updates, updated), "affected_count": len(updated)}

    def revoke_closure(self, actor: Actor, closure_id: int, expected_version: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_closure_action_role(actor, "revoke")
        closure = self.repository.get_closure(closure_id)
        if closure["status"] != "active":
            raise Conflict("封锁已撤销")
        updates = self._restore_updates(actor, "封锁已撤销，自动恢复原状态", exclude_closure_id=closure_id)
        details = {
            "summary": "封锁撤销",
            "kind": closure["kind"],
            "target": closure["target"],
            "restored": len(updates),
            "affected_count": len(updates),
            "plan_ids": [update["record_id"] for update in updates],
        }
        closure_row, updated = self.repository.update_closure_with_effects(closure_id, int(expected_version), {"status": "revoked"}, updates, "revoked", details, actor.user_id)
        return {"closure": closure_row, "affected": self._views(updates, updated), "affected_count": len(updated)}

    def list_closures(self, actor: Actor, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_closures(status=status, limit=limit)

    def get_closure(self, actor: Actor, closure_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        closure = self.repository.get_closure(closure_id)
        affected = []
        for record in self.repository.list_records(limit=500):
            marker = record["payload"].get("reschedule") or {}
            if marker.get("closure_reference") != closure["reference"]:
                continue
            affected.append({
                "record_id": record["id"],
                "reference": record["reference"],
                "vessel": record["payload"].get("vessel"),
                "state": record["state"],
                "reason": marker.get("reason"),
                "recovery": marker.get("recovery"),
                "mode": marker.get("mode"),
            })
        return {"closure": closure, "affected": affected, "affected_count": len(affected)}

    def closure_timeline(self, actor: Actor, closure_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.closure_timeline(closure_id)
