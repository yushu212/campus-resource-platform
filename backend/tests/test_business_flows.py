"""Behavior tests for the M1/M2/M3 integration and agent confirmation gate."""

from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from campus_app.clock import now_local
from campus_app.models import AuditEvent, Notification, Reservation, Room, Seat, User
from campus_app.security import daily_room_code

from .conftest import login


def future_start() -> str:
    return (
        (now_local() + timedelta(days=1))
        .replace(hour=10, minute=0, second=0, microsecond=0)
        .isoformat()
    )


def test_login_required_and_student_cannot_manage_rooms(system) -> None:
    client, _sessions = system
    assert client.get("/api/v1/rooms").status_code == 401
    login(client, "student")
    denied = client.post(
        "/api/v1/admin/rooms",
        json={
            "id": "NEW",
            "name": "New Room",
            "area": "North",
            "capacity": 12,
            "open_time": "08:00",
            "close_time": "22:00",
            "room_type": "study",
        },
    )
    assert denied.status_code == 403


def test_reservation_conflict_duration_and_idempotency(system) -> None:
    client, sessions = system
    login(client, "student")
    start = future_start()
    payload = {"seat_id": "A01", "start_at": start, "duration_hours": 2}
    headers = {"Idempotency-Key": "reserve-at-10"}
    first = client.post("/api/v1/reservations", json=payload, headers=headers)
    repeated = client.post("/api/v1/reservations", json=payload, headers=headers)
    assert first.status_code == 201
    assert repeated.status_code == 201
    assert repeated.json()["id"] == first.json()["id"]

    conflict_payload = {"seat_id": "A01", "start_at": start, "duration_hours": 1}
    conflict = client.post("/api/v1/reservations", json=conflict_payload)
    assert conflict.status_code == 409

    mismatched_key = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A02", "start_at": start, "duration_hours": 2},
        headers=headers,
    )
    assert mismatched_key.status_code == 409
    too_long = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A02", "start_at": start, "duration_hours": 5},
    )
    assert too_long.status_code == 422
    with sessions() as db:
        assert db.scalar(select(Reservation.id).where(Reservation.user_id == 1)) is not None


def test_classroom_hold_starts_only_after_second_approval(system) -> None:
    client, _sessions = system
    login(client, "student")
    tomorrow = now_local() + timedelta(days=1)
    start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
    end = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    application = client.post(
        "/api/v1/classroom-applications",
        json={
            "room_id": "C201",
            "start_at": start,
            "end_at": end,
            "purpose": "Course group meeting",
        },
    )
    assert application.status_code == 201
    application_id = application.json()["id"]

    def classroom_seats() -> dict[str, dict]:
        response = client.get(
            f"/api/v1/seats?date={tomorrow.date().isoformat()}&start_time=10:00&duration_hours=1&area=North+Campus"
        )
        assert response.status_code == 200
        return {row["id"]: row for row in response.json()["seats"]}

    assert classroom_seats()["C201-01"]["available"] is True
    login(client, "approver1")
    stage_one = client.post(
        f"/api/v1/classroom-applications/{application_id}/decision",
        json={"decision": "approve", "comment": "Stage one accepted"},
    )
    assert stage_one.status_code == 200
    assert classroom_seats()["C201-01"]["available"] is True
    login(client, "approver2")
    stage_two = client.post(
        f"/api/v1/classroom-applications/{application_id}/decision",
        json={"decision": "approve", "comment": "Stage two accepted"},
    )
    assert stage_two.status_code == 200
    blocked = classroom_seats()["C201-01"]
    assert blocked["available"] is False
    assert blocked["blocked_by"]["module"] == "M2"
    assert blocked["blocked_by"]["source_id"] == application_id


def test_repair_hold_is_added_at_start_and_released_at_acceptance(system) -> None:
    student, _sessions = system
    login(student, "student")
    report = student.post(
        "/api/v1/work-orders",
        json={
            "seat_id": "A02",
            "title": "Broken desk outlet",
            "description": "The outlet has no power.",
            "severity": "normal",
        },
    )
    assert report.status_code == 201
    work_order_id = report.json()["id"]

    repair = TestClient(student.app)
    login(repair, "repair")
    assigned = repair.post(f"/api/v1/work-orders/{work_order_id}/assign", json={"assignee_id": 5})
    assert assigned.status_code == 200
    started = repair.post(f"/api/v1/work-orders/{work_order_id}/start")
    assert started.status_code == 200
    tomorrow = (now_local() + timedelta(days=1)).date().isoformat()
    seats = repair.get(f"/api/v1/seats?date={tomorrow}&start_time=10:00&duration_hours=1").json()[
        "seats"
    ]
    broken = next(row for row in seats if row["id"] == "A02")
    assert broken["available"] is False
    assert broken["blocked_by"]["module"] == "M3"
    assert broken["blocked_by"]["source_id"] == work_order_id

    resolved = repair.post(f"/api/v1/work-orders/{work_order_id}/resolve")
    assert resolved.status_code == 200
    still_blocked = repair.get(
        f"/api/v1/seats?date={tomorrow}&start_time=10:00&duration_hours=1"
    ).json()["seats"]
    assert next(row for row in still_blocked if row["id"] == "A02")["available"] is False
    accepted = student.post(f"/api/v1/work-orders/{work_order_id}/accept")
    assert accepted.status_code == 200
    restored = student.get(
        f"/api/v1/seats?date={tomorrow}&start_time=10:00&duration_hours=1"
    ).json()["seats"]
    assert next(row for row in restored if row["id"] == "A02")["available"] is True
    rated = student.post(f"/api/v1/work-orders/{work_order_id}/rating", json={"rating": 5})
    assert rated.status_code == 200
    assert rated.json()["rating"] == 5
    assert [row["id"] for row in student.get("/api/v1/work-orders").json()] == [work_order_id]


def test_agent_write_requires_confirmation_and_creates_one_booking(system) -> None:
    client, sessions = system
    login(client, "student")
    preview = client.post(
        "/api/v1/agent/actions/preview",
        json={
            "action_type": "reserve_seat",
            "payload": {"seat_id": "A03", "start_at": future_start(), "duration_hours": 1},
        },
    )
    assert preview.status_code == 201
    action_id = preview.json()["id"]
    with sessions() as db:
        assert db.scalar(select(Reservation.id).where(Reservation.seat_id == "A03")) is None
    result = client.post(f"/api/v1/agent/actions/{action_id}/confirm", json={"confirmed": True})
    repeated = client.post(f"/api/v1/agent/actions/{action_id}/confirm", json={"confirmed": True})
    assert result.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["result"]["id"] == result.json()["result"]["id"]
    with sessions() as db:
        assert len(db.scalars(select(Reservation).where(Reservation.seat_id == "A03")).all()) == 1


def test_custom_role_permissions_are_saved_and_enforced(system) -> None:
    client, _sessions = system
    login(client, "admin")
    role = client.post(
        "/api/v1/admin/roles",
        json={
            "code": "classroom_clerk",
            "name": "教室申请查看员",
            "permissions": ["classroom:read_all", "notification:read"],
        },
    )
    assert role.status_code == 201
    account = client.post(
        "/api/v1/admin/users",
        json={
            "username": "clerk",
            "display_name": "Clerk",
            "password": "demo-pass-123",
            "role": "classroom_clerk",
        },
    )
    assert account.status_code == 201
    login(client, "clerk")
    current = client.get("/api/v1/auth/me").json()["user"]
    assert current["role_name"] == "教室申请查看员"
    assert "classroom:read_all" in current["permissions"]
    assert client.get("/api/v1/classroom-applications").status_code == 200
    denied = client.post(
        "/api/v1/classroom-applications/example/decision",
        json={"decision": "approve", "comment": ""},
    )
    assert denied.status_code == 403


def test_cancelled_approved_classroom_releases_all_seats(system) -> None:
    client, _sessions = system
    tomorrow = now_local() + timedelta(days=1)
    start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
    end = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    login(client, "student")
    created = client.post(
        "/api/v1/classroom-applications",
        json={"room_id": "C201", "start_at": start, "end_at": end, "purpose": "Project discussion"},
    )
    application_id = created.json()["id"]
    login(client, "approver1")
    assert (
        client.post(
            f"/api/v1/classroom-applications/{application_id}/decision",
            json={"decision": "approve", "comment": ""},
        ).status_code
        == 200
    )
    login(client, "approver2")
    assert (
        client.post(
            f"/api/v1/classroom-applications/{application_id}/decision",
            json={"decision": "approve", "comment": ""},
        ).status_code
        == 200
    )
    login(client, "student")
    cancelled = client.post(f"/api/v1/classroom-applications/{application_id}/cancel")
    assert cancelled.status_code == 200
    result = client.get(
        f"/api/v1/seats?date={tomorrow.date().isoformat()}&start_time=10:00&duration_hours=1"
    ).json()
    room_seats = [row for row in result["seats"] if row["room_id"] == "C201"]
    assert room_seats
    assert all(row["available"] for row in room_seats)


def test_work_order_can_be_cancelled_before_repair_starts(system) -> None:
    client, _sessions = system
    login(client, "student")
    created = client.post(
        "/api/v1/work-orders",
        json={
            "seat_id": "A01",
            "title": "Loose chair leg",
            "description": "The chair is unstable.",
            "severity": "low",
        },
    )
    work_order_id = created.json()["id"]
    cancelled = client.post(f"/api/v1/work-orders/{work_order_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_room_screen_token_shows_daily_code_and_cannot_log_in_as_user(system) -> None:
    client, _sessions = system
    login(client, "admin")
    created = client.post("/api/v1/admin/rooms/C201/screen-token")
    assert created.status_code == 200
    token = created.json()["screen_token"]
    screen = client.get(
        "/api/v1/rooms/C201/screen-code",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert screen.status_code == 200
    result = screen.json()
    assert result["code"] == daily_room_code("C201", now_local().date())
    assert screen.headers["cache-control"] == "no-store"
    assert (
        client.get(
            "/api/v1/rooms/C201/screen-code",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["code"]
        == result["code"]
    )
    assert (
        client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code
        == 401
    )
    assert (
        client.get(
            "/api/v1/rooms/LIB-1/screen-code",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code
        == 403
    )


def test_checkin_uses_today_room_code_not_a_reservation_secret(system) -> None:
    client, sessions = system
    with sessions() as db:
        student = db.scalar(select(User).where(User.username == "student"))
        now = now_local()
        db.add(
            Reservation(
                id="RSV-TODAY",
                user_id=student.id,
                seat_id="A03",
                start_at=now - timedelta(minutes=2),
                end_at=now + timedelta(minutes=58),
                duration_hours=1,
                status="confirmed",
            )
        )
        db.commit()
    login(client, "student")
    code = daily_room_code("LIB-1", now_local().date())
    result = client.post("/api/v1/reservations/RSV-TODAY/check-in", json={"code": code})
    assert result.status_code == 200
    assert result.json()["status"] == "checked_in"


def test_department_space_filter_and_admin_adjustable_booking_limit(system) -> None:
    client, sessions = system
    with sessions() as db:
        db.add(
            Room(
                id="LAW-1",
                name="Law Faculty Study Room",
                area="West Campus",
                room_type="study",
                access_scope="department",
                department_id="法学院",
                capacity=1,
                open_time="07:00",
                close_time="23:00",
            )
        )
        db.flush()
        db.add(Seat(id="LAW-01", room_id="LAW-1", label="LAW-01"))
        db.commit()

    login(client, "student")
    rooms = client.get("/api/v1/rooms").json()
    assert all(room["id"] != "LAW-1" for room in rooms)
    tomorrow = (now_local() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    denied = client.post(
        "/api/v1/reservations",
        json={"seat_id": "LAW-01", "start_at": tomorrow.isoformat(), "duration_hours": 1},
    )
    assert denied.status_code == 404

    login(client, "admin")
    update = client.put("/api/v1/admin/settings", json={"max_booking_hours": 6})
    assert update.status_code == 200
    login(client, "student")
    six_hours = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A01", "start_at": tomorrow.isoformat(), "duration_hours": 6},
    )
    assert six_hours.status_code == 201
    assert six_hours.json()["end_at"].endswith("16:00")
    too_long = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A02", "start_at": tomorrow.isoformat(), "duration_hours": 7},
    )
    assert too_long.status_code == 422


def test_notification_email_retries_without_exposing_smtp_errors(system, monkeypatch) -> None:
    _client, sessions = system
    current = now_local()
    with sessions() as db:
        student = db.scalar(select(User).where(User.username == "student"))
        student.email = "student@example.invalid"
        notice = Notification(
            user_id=student.id,
            kind="reservation",
            title="预约提醒",
            message="请准时签到。",
            related_id="RSV-1",
            unique_key="test:email:retry",
            email_queued_at=current,
        )
        db.add(notice)
        db.commit()
        notice_id = notice.id

    from campus_app import worker

    monkeypatch.setattr(worker, "SessionLocal", sessions)
    monkeypatch.setattr(worker, "SMTP_HOST", "mail.example.invalid")
    monkeypatch.setattr(worker, "SMTP_FROM", "noreply@example.invalid")
    send_calls = []

    def fake_send(_notification, recipient):
        send_calls.append(recipient)
        if len(send_calls) == 1:
            raise RuntimeError("private smtp password")

    monkeypatch.setattr(worker, "send_notification_email", fake_send)
    first = worker.deliver_pending_emails(current)
    assert first["emails_failed"] == 1
    with sessions() as db:
        notice = db.get(Notification, notice_id)
        assert notice.email_attempts == 1
        assert "private" not in notice.email_last_error
        assert notice.email_sent_at is None

    second = worker.deliver_pending_emails(current + timedelta(seconds=30))
    assert second["emails_sent"] == 1
    third = worker.deliver_pending_emails(current + timedelta(seconds=60))
    assert third["emails_sent"] == 0
    assert send_calls == ["student@example.invalid"] * 2
    with sessions() as db:
        notice = db.get(Notification, notice_id)
        assert notice.email_attempts == 2
        assert notice.email_sent_at is not None


def test_timeout_worker_releases_reservation_and_records_violation(system, monkeypatch) -> None:
    _client, sessions = system
    with sessions() as db:
        student = db.scalar(select(User).where(User.username == "student"))
        row = Reservation(
            id="RSV-LATE",
            user_id=student.id,
            seat_id="A03",
            start_at=now_local() - timedelta(minutes=16),
            end_at=now_local() + timedelta(minutes=44),
            duration_hours=1,
            status="confirmed",
        )
        db.add(row)
        db.commit()

    from campus_app import worker

    monkeypatch.setattr(worker, "SessionLocal", sessions)
    result = worker.process_due_events()
    assert result["no_shows"] == 1
    with sessions() as db:
        row = db.get(Reservation, "RSV-LATE")
        assert row.status == "expired"
        assert row.violation is True
        assert db.scalar(select(AuditEvent.id).where(AuditEvent.entity_id == row.id)) is not None
        assert (
            db.scalar(select(Notification.id).where(Notification.related_id == row.id)) is not None
        )


def test_m1_back_to_back_bookings_and_power_filter(system) -> None:
    """Verify M1-01 power filtering and M1-02 half-open interval [start, end) back-to-back bookings."""
    client, _sessions = system
    login(client, "student")
    tomorrow = (now_local() + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    date_str = tomorrow.date().isoformat()

    powered_resp = client.get(
        f"/api/v1/seats?date={date_str}&start_time=10:00&duration_hours=2&has_power=true"
    )
    assert powered_resp.status_code == 200
    powered_seats = powered_resp.json()["seats"]
    assert powered_seats
    assert all(seat["has_power"] is True for seat in powered_seats)

    first_slot = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A01", "start_at": tomorrow.isoformat(), "duration_hours": 2},
    )
    assert first_slot.status_code == 201

    adjacent_start = tomorrow.replace(hour=12)
    second_slot = client.post(
        "/api/v1/reservations",
        json={"seat_id": "A01", "start_at": adjacent_start.isoformat(), "duration_hours": 2},
    )
    assert second_slot.status_code == 201
    assert second_slot.json()["start_at"].endswith("12:00")
    assert second_slot.json()["end_at"].endswith("14:00")
