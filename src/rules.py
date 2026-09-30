"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "draft"
PENDING_STATE = "reschedule_pending"
RESTORABLE_STATES = ("draft", "confirmed")
CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {'confirm': {'port_controller'}, 'berth': {'port_controller'}, 'depart': {'port_controller'}, 'cancel': {'port_controller'}, 'reschedule': {'port_controller'}}
TRANSITIONS = {'confirm': {'draft': 'confirmed'}, 'berth': {'confirmed': 'berthed'}, 'depart': {'berthed': 'departed'}, 'cancel': {'draft': 'cancelled', 'confirmed': 'cancelled', 'reschedule_pending': 'cancelled'}}
CLOSURE_KINDS = ["berth_outage", "channel_closure"]
CLOSURE_ACTION_ROLES = {'adjust': {'port_controller'}, 'revoke': {'port_controller'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in CLOSURE_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_closure_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in CLOSURE_ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        channel = text(p, "channel")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, 23)
        etd = integer(p, "etd_hour", 1, 24)
        choice(p, "risk_level", ["low", "medium", "high"])
        dangerous = boolean(p, "dangerous_goods")
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        if berth_length < vessel_length:
            raise ValidationError("泊位长度不足")
        if berth_depth - draft < 0.5:
            raise ValidationError("剩余水深不足")
        if dangerous:
            text(p, "dangerous_class")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        p["safety_margin_m"] = round(float(p["berth_depth_m"]) - float(p["draft_m"]), 2)
        p["window_hours"] = int(p["etd_hour"]) - int(p["eta_hour"])
        p["quay_ok"] = bool(p["berth_length_m"] >= p["vessel_length_m"] and p["safety_margin_m"] >= 0.5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in {"cancelled", "departed"} or other.get("berth") != payload.get("berth"):
                continue
            if int(payload["eta_hour"]) < int(other.get("etd_hour", 0)) and int(payload["etd_hour"]) > int(other.get("eta_hour", 24)):
                raise Conflict("同一泊位时间窗冲突")

    def validate_closure(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        p["kind"] = choice(p, "kind", CLOSURE_KINDS)
        p["target"] = text(p, "target")
        p["start_hour"] = integer(p, "start_hour", 0, 23)
        p["end_hour"] = integer(p, "end_hour", 1, 24)
        p["reason"] = text(p, "reason")
        if p["end_hour"] <= p["start_hour"]:
            raise ValidationError("end_hour必须晚于start_hour")
        return p

    def validate_closure_adjust(self, payload: Dict[str, Any], closure: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        changes = {"start_hour": integer(p, "start_hour", 0, 23), "end_hour": integer(p, "end_hour", 1, 24)}
        if changes["end_hour"] <= changes["start_hour"]:
            raise ValidationError("end_hour必须晚于start_hour")
        changes["reason"] = optional_text(p, "reason") or closure["reason"]
        return changes

    def closure_overlap(self, closure: Dict[str, Any], plan_payload: Dict[str, Any]) -> bool:
        key = "berth" if closure.get("kind") == "berth_outage" else "channel"
        if str(plan_payload.get(key, "")) != str(closure.get("target", "")):
            return False
        return int(plan_payload["eta_hour"]) < int(closure["end_hour"]) and int(plan_payload["etd_hour"]) > int(closure["start_hour"])

    def closure_reason(self, closure: Dict[str, Any]) -> str:
        if closure.get("kind") == "berth_outage":
            return "泊位%s停用，与靠泊窗口冲突" % closure.get("target")
        return "航道%s封控，与靠泊窗口冲突" % closure.get("target")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        if action == "reschedule":
            if record["state"] != PENDING_STATE:
                raise Conflict("当前状态不允许执行%s" % action)
            previous = (record["payload"].get("reschedule") or {}).get("previous_state")
            if previous not in RESTORABLE_STATES:
                raise Conflict("缺少可恢复的原始状态")
            return previous
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "confirm":
            pilot = text(data, "pilot_id")
            changes["pilot_id"] = pilot
            summary = "已确认引航员"
        elif action == "berth":
            actual = number(data, "actual_draft_m", 0)
            if float(p["berth_depth_m"]) - actual < 0.5:
                raise ValidationError("实际吃水导致水深不足")
            changes["actual_draft_m"] = actual
            summary = "船舶已靠泊"
        elif action == "depart":
            if not boolean(data, "cargo_operation_complete"):
                raise ValidationError("货物作业尚未完成")
            summary = "船舶已离泊"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "计划已取消"
        elif action == "reschedule":
            eta = integer(data, "eta_hour", 0, 23)
            etd = integer(data, "etd_hour", 1, 24)
            if etd <= eta:
                raise ValidationError("etd_hour必须晚于eta_hour")
            changes["eta_hour"] = eta
            changes["etd_hour"] = etd
            changes["window_hours"] = etd - eta
            berth = optional_text(data, "berth")
            if berth:
                changes["berth"] = berth
            channel = optional_text(data, "channel")
            if channel:
                changes["channel"] = channel
            summary = "人工改期完成"
        p.update(changes)
        if action == "reschedule":
            marker = dict(p.get("reschedule") or {})
            marker["mode"] = "manual"
            marker["recovery"] = "manual"
            p["reschedule"] = marker
        return new_state, p, summary or ("已执行%s" % action)
