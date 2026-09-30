"""业务用例编排、权限检查、封锁联动的可恢复重调度与审计。

重调度模型
----------
计划主状态在原有 draft/confirmed/berthed/departed/cancelled 之上新增：

- ``reschedule_pending``：计划窗口与封锁冲突、且系统找不到可行窗口，计划失效待改期；
- ``auto_rescheduled``：系统自动避让到新窗口（封锁时段缩短/撤销后可恢复原窗口）；
- ``manual_rescheduled``：调度员人工改期落定，封锁变化不再自动回退。

``schedule_info`` 记录改期原因、原窗口、改期方式与关联封锁，使列表、统计、时间线
展示同一份调度结果。
"""
import copy
import hashlib
import json
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, optional_text, text
from .repository import Repository, _now
from .rules import (
    AUTO_RESCHEDULED,
    MANUAL_RESCHEDULED,
    RESCHEDULE_PENDING,
    TERMINAL_STATES,
    DomainRules,
)


def _request_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _kind_label(kind: str) -> str:
    return "泊位停用" if kind == "berth_outage" else "航道封控"


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    # ----- 身份与权限 -----

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _require_controller(actor: Actor) -> None:
        if actor.role != "admin" and "port_controller" not in actor.role:
            raise PermissionDenied("角色无权执行调度操作")

    def _finish_idempotent(
        self,
        connection,
        key: Optional[str],
        scope: str,
        request_payload: Dict[str, Any],
        response_code: int,
        response_body: Dict[str, Any],
    ) -> Dict[str, Any]:
        if key:
            self.repository.store_idempotent(
                connection,
                key=key,
                scope=scope,
                request_hash=_request_hash(request_payload),
                response_code=response_code,
                response_body=response_body,
                now=_now(),
            )
        return response_body

    # ----- 靠泊计划 -----

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        connection = self.repository.transaction()
        try:
            self.rules.check_create_conflicts(
                prepared,
                self.repository.list_records(limit=500, connection=connection),
                self.repository.active_blockades(connection),
            )
            record = self.repository.create(
                connection, reference, self.rules.INITIAL_STATE, prepared, actor.user_id, _now(),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return record

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
        connection = self.repository.transaction()
        try:
            record = self.repository.get(record_id, connection)
            new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
            result = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=action,
                details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
                connection=connection,
                schedule_info=record.get("schedule_info") if action in {"confirm", "berth", "depart"} else None,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return result

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ----- 人工改期 -----

    def reschedule_manual(
        self,
        actor: Actor,
        record_id: int,
        expected_version: int,
        data: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_controller(actor)
        eta, etd = self.rules.validate_manual_window(data or {})
        request_payload = {"record_id": record_id, "eta_hour": eta, "etd_hour": etd, "data": data or {}}
        reason = optional_text(data or {}, "reason") or "调度员人工改期"
        connection = self.repository.transaction()
        try:
            if idempotency_key:
                cached = self.repository.find_idempotent(connection, idempotency_key, _request_hash(request_payload))
                if cached:
                    connection.rollback()
                    return cached["response_body"]
            # 加锁后读取并校验状态与版本
            locked = self.repository.get(record_id, connection)
            if locked["state"] not in {RESCHEDULE_PENDING, AUTO_RESCHEDULED}:
                raise Conflict("仅待改期或自动改期的计划可人工改期")
            if int(locked["version"]) != int(expected_version):
                raise Conflict("版本冲突，请刷新后重试")
            blockades = self.repository.active_blockades(connection)
            records = self.repository.list_records(limit=500, connection=connection)
            if not self.rules.window_is_clear(
                eta, etd, locked["payload"].get("berth", ""), locked["payload"].get("channel", ""),
                records, blockades, exclude_id=record_id,
            ):
                raise Conflict("人工改期窗口仍与封锁或其它计划冲突")
            old_info = locked.get("schedule_info") or {}
            prior_state = old_info.get("prior_state") or ("confirmed" if locked["state"] == AUTO_RESCHEDULED else "draft")
            payload = copy.deepcopy(locked["payload"])
            payload["eta_hour"] = eta
            payload["etd_hour"] = etd
            payload["window_hours"] = etd - eta
            info = {
                "mode": "manual",
                "reason": reason,
                "prior_state": prior_state,
                "original_eta_hour": old_info.get("original_eta_hour", locked["payload"]["eta_hour"]),
                "original_etd_hour": old_info.get("original_etd_hour", locked["payload"]["etd_hour"]),
                "related_blockade_ids": old_info.get("related_blockade_ids", []),
                "reschedulable": False,
            }
            updated = self.repository.mutate(
                record_id=record_id,
                expected_version=int(expected_version),
                state=MANUAL_RESCHEDULED,
                payload=payload,
                actor_id=actor.user_id,
                action="reschedule",
                details={
                    "summary": reason,
                    "mode": "manual",
                    "from": locked["state"],
                    "to": MANUAL_RESCHEDULED,
                    "eta_hour": eta,
                    "etd_hour": etd,
                    "reason": reason,
                },
                connection=connection,
                schedule_info=info,
            )
            body = {"record": updated, "recovery_status": "manual_locked", "affected_count": 1}
            self._finish_idempotent(connection, idempotency_key, "reschedule", request_payload, 200, body)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return body

    # ----- 封锁登记 / 变更 / 撤销 -----

    def register_blockade(
        self, actor: Actor, payload: Dict[str, Any], idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_controller(actor)
        fields = self.rules.validate_blockade(payload or {})
        request_payload = {"action": "register", **fields}
        connection = self.repository.transaction()
        try:
            if idempotency_key:
                cached = self.repository.find_idempotent(connection, idempotency_key, _request_hash(request_payload))
                if cached:
                    connection.rollback()
                    return cached["response_body"]
            records = self.repository.list_records(limit=500, connection=connection)
            blockades = self.repository.active_blockades(connection)
            blockade = self.repository.create_blockade(connection, fields, actor.user_id, _now())
            items = self._reconcile(
                connection, actor, blockade, blockades + [blockade], records,
                trigger="registered", trigger_detail=fields,
            )
            body = {
                "blockade": self.repository.get_blockade(blockade["id"], connection),
                "items": items,
                "affected_count": len(items),
                "replayed": False,
            }
            self._finish_idempotent(connection, idempotency_key, "blockade", request_payload, 200, body)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return body

    def change_blockade_window(
        self,
        actor: Actor,
        blockade_id: int,
        payload: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_controller(actor)
        data = dict(payload or {})
        from .domain import integer as _integer
        start_hour = _integer(data, "start_hour", 0, self.rules.HORIZON_HOUR - 1)
        end_hour = _integer(data, "end_hour", 1, self.rules.HORIZON_HOUR)
        if end_hour <= start_hour:
            raise ValidationError("end_hour必须晚于start_hour")
        connection = self.repository.transaction()
        try:
            blockade = self.repository.get_blockade(blockade_id, connection)
            if blockade["status"] != "active":
                raise Conflict("仅生效中的封锁可调整时段")
            request_payload = {
                "action": "change_window", "id": blockade_id,
                "start_hour": start_hour, "end_hour": end_hour,
            }
            if idempotency_key:
                cached = self.repository.find_idempotent(connection, idempotency_key, _request_hash(request_payload))
                if cached:
                    connection.rollback()
                    return cached["response_body"]
            if start_hour == int(blockade["start_hour"]) and end_hour == int(blockade["end_hour"]):
                body = {
                    "blockade": self.repository.get_blockade(blockade_id, connection),
                    "items": [],
                    "affected_count": 0,
                    "replayed": False,
                }
            else:
                updated = self.repository.update_blockade(
                    connection, blockade, start_hour, end_hour, actor.user_id,
                    action="blockade_window_changed",
                    details={
                        "from": {"start_hour": blockade["start_hour"], "end_hour": blockade["end_hour"]},
                        "to": {"start_hour": start_hour, "end_hour": end_hour},
                    },
                )
                records = self.repository.list_records(limit=500, connection=connection)
                others = [b for b in self.repository.active_blockades(connection) if int(b["id"]) != blockade_id]
                items = self._reconcile(
                    connection, actor, updated, others + [updated], records,
                    trigger="window_changed",
                    trigger_detail={
                        "from": {"start_hour": blockade["start_hour"], "end_hour": blockade["end_hour"]},
                        "to": {"start_hour": start_hour, "end_hour": end_hour},
                    },
                )
                body = {
                    "blockade": self.repository.get_blockade(blockade_id, connection),
                    "items": items,
                    "affected_count": len(items),
                    "replayed": False,
                }
            self._finish_idempotent(connection, idempotency_key, "blockade", request_payload, 200, body)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return body

    def revoke_blockade(
        self, actor: Actor, blockade_id: int, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._require_controller(actor)
        request_payload = {"action": "revoke", "id": blockade_id}
        connection = self.repository.transaction()
        try:
            if idempotency_key:
                cached = self.repository.find_idempotent(connection, idempotency_key, _request_hash(request_payload))
                if cached:
                    connection.rollback()
                    return cached["response_body"]
            blockade = self.repository.get_blockade(blockade_id, connection)
            if blockade["status"] != "active":
                raise Conflict("封锁已撤销，无需重复操作")
            others = [b for b in self.repository.active_blockades(connection) if int(b["id"]) != blockade_id]
            # 先重算（按本锁已消失），再把封锁标记撤销，保证审计顺序为：计划恢复 -> 封锁撤销。
            records = self.repository.list_records(limit=500, connection=connection)
            items = self._reconcile(
                connection, actor, blockade, others, records,
                trigger="revoked", trigger_detail={},
            )
            updated = self.repository.update_blockade(
                connection, blockade, int(blockade["start_hour"]), int(blockade["end_hour"]),
                actor.user_id, action="blockade_revoked", details={}, status="revoked",
            )
            body = {
                "blockade": updated,
                "items": items,
                "affected_count": len(items),
                "replayed": False,
            }
            self._finish_idempotent(connection, idempotency_key, "blockade", request_payload, 200, body)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return body

    def list_blockades(
        self, actor: Actor, status: Optional[str] = None, kind: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_blockades(status=status, kind=kind, limit=limit)

    def get_blockade(self, actor: Actor, blockade_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_blockade(blockade_id)

    def blockade_timeline(self, actor: Actor, blockade_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.blockade_timeline(blockade_id)

    # ----- 重调度核心 -----

    @classmethod
    def _conflict_blockade_ids(cls, payload: Dict[str, Any], blockades: List[Dict[str, Any]]) -> List[int]:
        ids: List[int] = []
        for blockade in blockades:
            if blockade.get("status") != "active":
                continue
            if payload.get("berth") == blockade["target"] and blockade["kind"] == "berth_outage":
                if payload["eta_hour"] < int(blockade["end_hour"]) and int(blockade["start_hour"]) < payload["etd_hour"]:
                    ids.append(int(blockade["id"]))
            elif payload.get("channel") == blockade["target"] and blockade["kind"] == "channel_closure":
                if payload["eta_hour"] < int(blockade["end_hour"]) and int(blockade["start_hour"]) < payload["etd_hour"]:
                    ids.append(int(blockade["id"]))
        return ids

    def _reconcile(
        self,
        connection,
        actor: Actor,
        trigger_blockade: Dict[str, Any],
        active_blockades: List[Dict[str, Any]],
        records: List[Dict[str, Any]],
        trigger: str,
        trigger_detail: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """按触发封锁重算全部受影响计划，返回每个计划的改期原因/恢复状态/受影响数量。

        - 已靠泊/离泊/取消计划不动；
        - 自动改期(auto_rescheduled)优先尝试恢复原窗口：清空则恢复，仍被占用则重新自动避让，
          找不到窗口则落入待改期；
        - 人工改期(manual_rescheduled)不随封锁变化回退：窗口仍安全则不动，撞锁则失效待改期；
        - 待改期(reschedule_pending)保持失效，直到人工改期；
        - 草稿/已确认计划撞锁先尝试自动避让，失败才失效待改期。
        """
        items: List[Dict[str, Any]] = []
        kind_label = _kind_label(trigger_blockade["kind"])
        by_id = {int(r["id"]): r for r in records}

        for record in records:
            state = record["state"]
            if state in TERMINAL_STATES:
                continue
            payload = record["payload"]
            if not self._related(payload, trigger_blockade):
                continue
            info = record.get("schedule_info") or {}
            original_eta = info.get("original_eta_hour", payload["eta_hour"])
            original_etd = info.get("original_etd_hour", payload["etd_hour"])
            prior_state = info.get("prior_state")
            related_ids = sorted(set(info.get("related_blockade_ids", [])) | {int(trigger_blockade["id"])})

            if state == MANUAL_RESCHEDULED:
                if self._conflict_blockade_ids(payload, active_blockades):
                    new_record = self._to_pending(
                        connection, actor, record, active_blockades,
                        reason="人工改期窗口仍撞上%s，计划失效待改期" % kind_label,
                        related_ids=related_ids, trigger=trigger, trigger_detail=trigger_detail,
                        trigger_blockade=trigger_blockade,
                    )
                    items.append(self._item(new_record, "manual_invalidated", "人工改期后再次撞上封锁，已失效等待重新改期"))
                continue

            if state == RESCHEDULE_PENDING:
                continue

            if state == AUTO_RESCHEDULED:
                # 优先恢复原窗口（仅自动改期的计划可回退）。
                clear_original = self.rules.window_is_clear(
                    int(original_eta), int(original_etd),
                    payload.get("berth", ""), payload.get("channel", ""),
                    records, active_blockades, exclude_id=int(record["id"]),
                )
                if clear_original:
                    new_record = self._restore(
                        connection, actor, record, int(original_eta), int(original_etd),
                        prior_state or "draft", related_ids, trigger, trigger_detail, trigger_blockade,
                    )
                    items.append(self._item(new_record, "restored", "封锁缩短/撤销，自动改期计划已恢复原窗口"))
                    by_id[int(record["id"])] = new_record
                    records = [by_id[int(r["id"])] for r in records]
                    continue
                # 原窗口仍不可用：重新自动避让。
                # 新登记/延长从触发封锁结束点之后避让；缩短/撤销从剩余冲突封锁的最早
                # 结束点回挪，使避让窗口随封锁解除及时前移；找不到窗口才落入待改期。
                original_view = {
                    "eta_hour": int(original_eta), "etd_hour": int(original_etd),
                    "berth": payload.get("berth"), "channel": payload.get("channel"),
                }
                if trigger == "registered":
                    anchor = int(trigger_blockade["end_hour"])
                else:
                    anchor = self._earliest_conflict_end(original_view, active_blockades)
                new_record = self._auto_move(
                    connection, actor, record, active_blockades, records,
                    reason="%s时段变化，按最新封锁时段重新自动避让" % kind_label,
                    original_eta=int(original_eta), original_etd=int(original_etd),
                    prior_state=prior_state or "draft", related_ids=related_ids,
                    trigger=trigger, trigger_detail=trigger_detail, trigger_blockade=trigger_blockade,
                    force_earliest=anchor,
                )
                by_id[int(record["id"])] = new_record
                records = [by_id[int(r["id"])] for r in records]
                if new_record["state"] == AUTO_RESCHEDULED:
                    if (new_record["payload"]["eta_hour"], new_record["payload"]["etd_hour"]) != (
                        payload["eta_hour"], payload["etd_hour"]
                    ):
                        items.append(self._item(new_record, "auto_reshifted", "封锁时段变化，自动改期窗口已重新避让"))
                else:
                    items.append(self._item(new_record, "auto_failed_pending", "自动避让找不到可行窗口，计划失效待改期"))
                continue

            # draft / confirmed
            if state in {"draft", "confirmed"} and self._conflict_blockade_ids(payload, active_blockades):
                new_record = self._auto_move(
                    connection, actor, record, active_blockades, records,
                    reason="%s导致原窗口不可用，系统自动改期避让" % kind_label,
                    original_eta=int(payload["eta_hour"]), original_etd=int(payload["etd_hour"]),
                    prior_state=state, related_ids=related_ids,
                    trigger=trigger, trigger_detail=trigger_detail, trigger_blockade=trigger_blockade,
                )
                by_id[int(record["id"])] = new_record
                records = [by_id[int(r["id"])] for r in records]
                if new_record["state"] == AUTO_RESCHEDULED:
                    items.append(self._item(new_record, "auto_rescheduled", "原窗口撞上%s，已自动避让，封锁解除后可恢复" % kind_label))
                else:
                    items.append(self._item(new_record, "pending", "撞上%s且无可行避让窗口，计划失效待改期" % kind_label))

        return items

    @staticmethod
    def _related(payload: Dict[str, Any], blockade: Dict[str, Any]) -> bool:
        if blockade["kind"] == "berth_outage":
            return payload.get("berth") == blockade["target"]
        return payload.get("channel") == blockade["target"]

    @classmethod
    def _earliest_conflict_end(cls, payload: Dict[str, Any], blockades: List[Dict[str, Any]]) -> int:
        """与给定窗口冲突的全部活跃封锁中最早的结束点；无冲突时返回窗口起点。"""
        ends = [int(b["end_hour"]) for b in blockades if cls._conflict_blockade_ids(payload, [b])]
        return min(ends) if ends else int(payload["eta_hour"])

    def _item(self, record: Dict[str, Any], recovery_status: str, recovery_message: str) -> Dict[str, Any]:
        info = record.get("schedule_info") or {}
        return {
            "record_id": record["id"],
            "reference": record["reference"],
            "state": record["state"],
            "version": record["version"],
            "reschedule_reason": info.get("reason", ""),
            "recovery_status": recovery_status,
            "recovery_message": recovery_message,
            "reschedulable": bool(info.get("reschedulable", False)),
            "mode": info.get("mode"),
            "original_window": {
                "eta_hour": info.get("original_eta_hour", record["payload"]["eta_hour"]),
                "etd_hour": info.get("original_etd_hour", record["payload"]["etd_hour"]),
            },
            "current_window": {
                "eta_hour": record["payload"]["eta_hour"],
                "etd_hour": record["payload"]["etd_hour"],
            },
            "affected_count": 1,
            "related_blockade_ids": info.get("related_blockade_ids", []),
        }

    def _audit_detail(
        self, action: str, record: Dict[str, Any], to_state: str, reason: str,
        trigger: str, trigger_detail: Dict[str, Any], trigger_blockade: Dict[str, Any],
        **extra: Any
    ) -> Dict[str, Any]:
        detail = {
            "summary": reason,
            "from": record["state"],
            "to": to_state,
            "reason": reason,
            "trigger": trigger,
            "trigger_blockade_id": int(trigger_blockade["id"]),
            "trigger_blockade_kind": trigger_blockade["kind"],
            "blockade_id": int(trigger_blockade["id"]),
        }
        detail.update(trigger_detail)
        detail.update(extra)
        return detail

    def _auto_move(
        self, connection, actor, record, active_blockades, records, *,
        reason: str, original_eta: int, original_etd: int, prior_state: str,
        related_ids: List[int], trigger: str, trigger_detail: Dict[str, Any],
        trigger_blockade: Dict[str, Any], force_earliest: Optional[int] = None,
    ) -> Dict[str, Any]:
        conflicts = self._conflict_blockade_ids(record["payload"], active_blockades)
        if force_earliest is not None:
            earliest = int(force_earliest)
        elif conflicts:
            earliest = min(
                int(b["end_hour"]) for b in active_blockades
                if self._related(record["payload"], b) and self._conflict_blockade_ids(record["payload"], [b])
            )
        else:
            earliest = int(record["payload"]["eta_hour"])
        slot = self.rules.find_free_slot(
            record, earliest,
            [r for r in records if int(r["id"]) != int(record["id"])],
            active_blockades,
        )
        payload = copy.deepcopy(record["payload"])
        if slot is None:
            info = {
                "mode": "pending",
                "reason": reason,
                "prior_state": prior_state,
                "original_eta_hour": int(original_eta),
                "original_etd_hour": int(original_etd),
                "related_blockade_ids": related_ids,
                "reschedulable": False,
            }
            return self.repository.force_record_change(
                connection, record, RESCHEDULE_PENDING, payload, info, actor.user_id,
                action="schedule_invalidated",
                details=self._audit_detail(
                    "schedule_invalidated", record, RESCHEDULE_PENDING, reason,
                    trigger, trigger_detail, trigger_blockade,
                ),
            )
        eta, etd = slot
        payload["eta_hour"] = eta
        payload["etd_hour"] = etd
        payload["window_hours"] = etd - eta
        info = {
            "mode": "auto",
            "reason": reason,
            "prior_state": prior_state,
            "original_eta_hour": int(original_eta),
            "original_etd_hour": int(original_etd),
            "related_blockade_ids": related_ids,
            "reschedulable": True,
        }
        return self.repository.force_record_change(
            connection, record, AUTO_RESCHEDULED, payload, info, actor.user_id,
            action="schedule_auto_rescheduled",
            details=self._audit_detail(
                "schedule_auto_rescheduled", record, AUTO_RESCHEDULED, reason,
                trigger, trigger_detail, trigger_blockade,
                eta_hour=eta, etd_hour=etd,
            ),
        )

    def _to_pending(
        self, connection, actor, record, active_blockades, *,
        reason: str, related_ids: List[int], trigger: str,
        trigger_detail: Dict[str, Any], trigger_blockade: Dict[str, Any],
    ) -> Dict[str, Any]:
        info = record.get("schedule_info") or {}
        new_info = {
            "mode": "pending",
            "reason": reason,
            "prior_state": info.get("prior_state", record["state"]),
            "original_eta_hour": int(info.get("original_eta_hour", record["payload"]["eta_hour"])),
            "original_etd_hour": int(info.get("original_etd_hour", record["payload"]["etd_hour"])),
            "related_blockade_ids": related_ids,
            "reschedulable": False,
        }
        return self.repository.force_record_change(
            connection, record, RESCHEDULE_PENDING, copy.deepcopy(record["payload"]), new_info,
            actor.user_id, action="schedule_invalidated",
            details=self._audit_detail(
                "schedule_invalidated", record, RESCHEDULE_PENDING, reason,
                trigger, trigger_detail, trigger_blockade,
            ),
        )

    def _restore(
        self, connection, actor, record, eta: int, etd: int, prior_state: str,
        related_ids: List[int], trigger: str, trigger_detail: Dict[str, Any],
        trigger_blockade: Dict[str, Any],
    ) -> Dict[str, Any]:
        reason = "%s解除或缩短，自动改期计划恢复原窗口" % _kind_label(trigger_blockade["kind"])
        payload = copy.deepcopy(record["payload"])
        payload["eta_hour"] = eta
        payload["etd_hour"] = etd
        payload["window_hours"] = etd - eta
        return self.repository.force_record_change(
            connection, record, prior_state, payload, None, actor.user_id,
            action="schedule_restored",
            details=self._audit_detail(
                "schedule_restored", record, prior_state, reason,
                trigger, trigger_detail, trigger_blockade,
                eta_hour=eta, etd_hour=etd, restored_to=prior_state,
            ),
        )
