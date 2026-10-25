"""Canvas-like connector — legacy dialect kept working (field maps moved
from sis_adapter per C.1).  Rows arrive via the batch/webhook HTTP contracts."""

from __future__ import annotations

from typing import Any, Optional

from connectors.base import CanonicalBatch, SourceConnector
from connectors.canvas.field_maps import DETECT_KEYS

_ENTITY_HINTS = (
    ("attendance", ("attendance_date", "status")),
    ("fees", ("amount_due", "amount_paid", "due_date")),
    ("staff", ("subjects", "max_periods", "is_available")),
)


class CanvasConnector(SourceConnector):
    dialect = "canvas"

    @classmethod
    def detect(cls, payload: dict[str, Any]) -> bool:
        return bool(DETECT_KEYS & set(payload.keys()))

    def pull(self, since: Optional[str] = None) -> CanonicalBatch:
        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        batch.errors.append("canvas: pull() not supported — use POST /api/v1/sis/sync")
        self._mark_error(RuntimeError("pull not supported"))
        return batch

    def webhook(self, payload: dict[str, Any]) -> CanonicalBatch:
        import sis_adapter

        batch = CanonicalBatch(source=self.dialect, school_id=self.school_id)
        data = payload.get("data") or payload
        event = str(payload.get("event") or "").lower()
        kind = None
        for name, keys in _ENTITY_HINTS:
            if any(k in data for k in keys):
                kind = name
                break
        if "staff" in event or "user" in event and "sis_user_id" not in data:
            kind = kind or "staff"
        elif "fee" in event:
            kind = "fees"
        elif "attendance" in event:
            kind = "attendance"
        try:
            if kind == "staff":
                batch.staff.append(sis_adapter.map_staff(data, self.dialect, self.school_id))
            elif kind == "fees":
                batch.fees.append(sis_adapter.map_fee(data, self.dialect, self.school_id))
            elif kind == "attendance":
                batch.attendance.append(
                    sis_adapter.map_attendance(data, self.dialect, self.school_id))
            else:
                batch.students.append(
                    sis_adapter.map_student(data, self.dialect, self.school_id))
            self._mark_success()
        except Exception as exc:
            self._mark_error(exc)
            batch.errors.append(f"{type(exc).__name__}: {exc}")
        return batch

    def health(self) -> dict[str, Any]:
        base = super().health()
        base["mode"] = "webhook"
        base["status"] = "OK"
        return base
