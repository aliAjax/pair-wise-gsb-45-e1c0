"""港口泊位与航道调度领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Conflict, ValidationError, boolean, choice, integer, number, text


INITIAL_STATE = "draft"
# 调度时间轴以“小时”为单位，允许跨日（0 ~ HORIZON_HOUR）。
HORIZON_HOUR = 24 * 30

CREATE_ROLES = {'port_controller'}
ACTION_ROLES = {
    'confirm': {'port_controller'},
    'berth': {'port_controller'},
    'depart': {'port_controller'},
    'cancel': {'port_controller'},
    'reschedule': {'port_controller'},
}
# 计划主状态：
# draft/confirmed 为正常可执行计划；reschedule_pending 为失效待改期；
# auto_rescheduled 为系统自动避让（封锁解除可回退）；manual_rescheduled 为人工改期落定（不回退）。
RESCHEDULE_PENDING = "reschedule_pending"
AUTO_RESCHEDULED = "auto_rescheduled"
MANUAL_RESCHEDULED = "manual_rescheduled"
TERMINAL_STATES = {"berthed", "departed", "cancelled"}

TRANSITIONS = {
    'confirm': {'draft': 'confirmed', MANUAL_RESCHEDULED: 'confirmed'},
    'berth': {'confirmed': 'berthed', MANUAL_RESCHEDULED: 'berthed'},
    'depart': {'berthed': 'departed'},
    'cancel': {
        'draft': 'cancelled',
        'confirmed': 'cancelled',
        RESCHEDULE_PENDING: 'cancelled',
        AUTO_RESCHEDULED: 'cancelled',
        MANUAL_RESCHEDULED: 'cancelled',
    },
    # 待改期计划或系统自动改期计划被调度员人工接管后，进入人工改期状态。
    'reschedule': {RESCHEDULE_PENDING: MANUAL_RESCHEDULED, AUTO_RESCHEDULED: MANUAL_RESCHEDULED},
}

BLOCKADE_KINDS = {"berth_outage": "berth", "channel_closure": "channel"}


def windows_overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    """半开区间 [start, end) 是否重叠。"""
    return start_a < end_b and start_b < end_a


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    HORIZON_HOUR = HORIZON_HOUR

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        vessel = text(p, "vessel")
        berth = text(p, "berth")
        channel = text(p, "channel")
        vessel_length = number(p, "vessel_length_m", 1)
        berth_length = number(p, "berth_length_m", 1)
        draft = number(p, "draft_m", 0)
        berth_depth = number(p, "berth_depth_m", 0)
        eta = integer(p, "eta_hour", 0, HORIZON_HOUR - 1)
        etd = integer(p, "etd_hour", 1, HORIZON_HOUR)
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

    @staticmethod
    def _hits_blockade(payload: Dict[str, Any], kind: str, target: str, start_hour: int, end_hour: int) -> bool:
        if kind == "berth_outage":
            if payload.get("berth") != target:
                return False
        elif kind == "channel_closure":
            if payload.get("channel") != target:
                return False
        else:
            return False
        return windows_overlap(
            int(payload["eta_hour"]), int(payload["etd_hour"]), int(start_hour), int(end_hour)
        )

    def check_create_conflicts(
        self,
        payload: Dict[str, Any],
        existing: Iterable[Dict[str, Any]],
        active_blockades: Iterable[Dict[str, Any]] = (),
    ) -> None:
        for item in existing:
            other = item["payload"]
            if item["state"] in TERMINAL_STATES:
                continue
            same_berth = other.get("berth") == payload.get("berth")
            same_channel = other.get("channel") == payload.get("channel")
            if not (same_berth or same_channel):
                continue
            if windows_overlap(
                int(payload["eta_hour"]),
                int(payload["etd_hour"]),
                int(other.get("eta_hour", 0)),
                int(other.get("etd_hour", HORIZON_HOUR)),
            ):
                if same_berth:
                    raise Conflict("同一泊位时间窗冲突")
                raise Conflict("同一航道时间窗冲突")
        for blockade in active_blockades:
            if self._hits_blockade(
                payload, blockade["kind"], blockade["target"], blockade["start_hour"], blockade["end_hour"]
            ):
                if blockade["kind"] == "berth_outage":
                    raise Conflict("计划窗口撞上浮泊位停用")
                raise Conflict("计划窗口撞上浮航道封控")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        if action in {"confirm", "berth"} and record["state"] == MANUAL_RESCHEDULED:
            info = record.get("schedule_info") or {}
            prior = info.get("prior_state")
            expected_prior = "draft" if action == "confirm" else "confirmed"
            if prior != expected_prior:
                raise Conflict("人工改期前的计划状态不允许执行%s" % action)
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
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ----- 封锁与改期 -----

    def validate_blockade(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload or {})
        kind = choice(p, "kind", list(BLOCKADE_KINDS))
        target = text(p, "target")
        start_hour = integer(p, "start_hour", 0, HORIZON_HOUR - 1)
        end_hour = integer(p, "end_hour", 1, HORIZON_HOUR)
        if end_hour <= start_hour:
            raise ValidationError("end_hour必须晚于start_hour")
        reason = p.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise ValidationError("reason必须是文本")
        return {
            "kind": kind,
            "target": target,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "reason": reason.strip(),
        }

    def validate_manual_window(self, data: Dict[str, Any]) -> Tuple[int, int]:
        data = dict(data or {})
        if "eta_hour" not in data:
            raise ValidationError("人工改期必须提供eta_hour")
        eta = integer(data, "eta_hour", 0, HORIZON_HOUR - 1)
        etd = integer(data, "etd_hour", 1, HORIZON_HOUR)
        if etd <= eta:
            raise ValidationError("etd_hour必须晚于eta_hour")
        return eta, etd

    def blockade_matches(self, record_payload: Dict[str, Any], blockade: Dict[str, Any]) -> bool:
        return self._hits_blockade(
            record_payload,
            blockade["kind"],
            blockade["target"],
            int(blockade["start_hour"]),
            int(blockade["end_hour"]),
        )

    def window_is_clear(
        self,
        eta: int,
        etd: int,
        berth: str,
        channel: str,
        records: Iterable[Dict[str, Any]],
        blockades: Iterable[Dict[str, Any]],
        exclude_id: Optional[int] = None,
    ) -> bool:
        """窗口是否与同泊位/同航道计划及活跃封锁全部错开。"""
        for blockade in blockades:
            if blockade.get("status") != "active":
                continue
            same = (blockade["kind"] == "berth_outage" and blockade["target"] == berth) or (
                blockade["kind"] == "channel_closure" and blockade["target"] == channel
            )
            if same and windows_overlap(eta, etd, int(blockade["start_hour"]), int(blockade["end_hour"])):
                return False
        for record in records:
            if exclude_id is not None and int(record["id"]) == int(exclude_id):
                continue
            if record["state"] in TERMINAL_STATES:
                continue
            other = record["payload"]
            same_berth = other.get("berth") == berth
            same_channel = other.get("channel") == channel
            if not (same_berth or same_channel):
                continue
            if windows_overlap(
                eta, etd, int(other.get("eta_hour", 0)), int(other.get("etd_hour", HORIZON_HOUR))
            ):
                return False
        return True

    def find_free_slot(
        self,
        record: Dict[str, Any],
        earliest: int,
        records: Iterable[Dict[str, Any]],
        blockades: Iterable[Dict[str, Any]],
    ) -> Optional[Tuple[int, int]]:
        """在不早于 earliest 的位置寻找同等时长、不撞封锁与其它计划的最早窗口。"""
        payload = record["payload"]
        length = int(payload["etd_hour"]) - int(payload["eta_hour"])
        start = max(0, int(earliest))
        records = list(records)
        blockades = list(blockades)
        eta = start
        while eta + length <= HORIZON_HOUR:
            if self.window_is_clear(
                eta,
                eta + length,
                payload.get("berth", ""),
                payload.get("channel", ""),
                records,
                blockades,
                exclude_id=record["id"],
            ):
                return eta, eta + length
            eta += 1
        return None
