"""Unit tests for the stage 6 (queues + SLA) additions to
`app.models.enums`: `SlaEventType` and `NotificationKind`. Values must
match the labels created for their corresponding Postgres enum types in
`app/alembic/versions/0008_queues_sla_stage6.py` exactly — same convention
already documented at the top of `app/models/enums.py`.
"""

from app.models.enums import NotificationKind, SlaEventType


def test_sla_event_type_values() -> None:
    assert SlaEventType.WARNING.value == "warning"
    assert SlaEventType.BREACHED.value == "breached"
    assert SlaEventType.RESOLVED.value == "resolved"
    assert [member.value for member in SlaEventType] == ["warning", "breached", "resolved"]


def test_notification_kind_values() -> None:
    assert NotificationKind.SLA_WARNING.value == "sla_warning"
    assert NotificationKind.SLA_BREACHED.value == "sla_breached"
    assert [member.value for member in NotificationKind] == ["sla_warning", "sla_breached"]


def test_sla_event_type_and_notification_kind_are_str_enums() -> None:
    assert issubclass(SlaEventType, str)
    assert issubclass(NotificationKind, str)
