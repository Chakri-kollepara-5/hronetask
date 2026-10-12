from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from fastapi.testclient import TestClient

import app.main as main


@pytest.fixture
def client():
    main.client = mongomock.MongoClient()
    main.db = main.client["attendance_db"]
    main.ensure_indexes()
    with TestClient(main.app) as test_client:
        yield test_client


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def create_employee(client, emp_code, shift_start="09:30", shift_end="18:30", joined_on="2026-01-05"):
    payload = {
        "emp_code": emp_code,
        "name": f"Employee {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": "Engineering",
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    }
    return client.post("/employees", json=payload)


def test_duplicate_employee_and_punch_in_are_rejected(client):
    r1 = create_employee(client, "EMP0001")
    assert r1.status_code == 201, r1.text

    r2 = create_employee(client, "EMP0001")
    assert r2.status_code == 409, r2.text

    punch_ts = datetime(2026, 1, 5, 4, 10, 1, tzinfo=timezone.utc)
    r3 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": to_ms(punch_ts)})
    assert r3.status_code == 201, r3.text

    r4 = client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": to_ms(punch_ts)})
    assert r4.status_code == 409, r4.text


def test_late_minutes_follow_contract_boundaries(client):
    create_employee(client, "EMP0002", shift_start="09:30", shift_end="18:30", joined_on="2026-01-05")
    punch_ok = datetime(2026, 1, 5, 4, 10, 0, tzinfo=timezone.utc)
    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": to_ms(punch_ok)})
    assert r1.status_code == 201, r1.text
    assert r1.json()["late_minutes"] == 0

    create_employee(client, "EMP0003", shift_start="09:30", shift_end="18:30", joined_on="2026-01-05")
    punch_late = datetime(2026, 1, 5, 4, 10, 1, tzinfo=timezone.utc)
    r2 = client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": to_ms(punch_late)})
    assert r2.status_code == 201, r2.text
    assert r2.json()["late_minutes"] == 10


def test_overtime_and_half_day_follow_contract(client):
    create_employee(client, "EMP0004", shift_start="09:00", shift_end="18:00", joined_on="2026-01-05")
    punch_in = datetime(2026, 1, 5, 3, 30, 0, tzinfo=timezone.utc)
    punch_out_29 = datetime(2026, 1, 5, 12, 59, 0, tzinfo=timezone.utc)
    punch_out_30 = datetime(2026, 1, 6, 13, 0, 0, tzinfo=timezone.utc)

    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": to_ms(punch_in)})
    assert r1.status_code == 201, r1.text
    r2 = client.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": to_ms(punch_out_29)})
    assert r2.status_code == 200, r2.text
    assert r2.json()["overtime_minutes"] == 0
    assert r2.json()["half_day"] is False

    create_employee(client, "EMP0005", shift_start="09:00", shift_end="18:00", joined_on="2026-01-06")
    punch_in2 = datetime(2026, 1, 6, 3, 30, 0, tzinfo=timezone.utc)
    r3 = client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": to_ms(punch_in2)})
    assert r3.status_code == 201, r3.text
    r4 = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": to_ms(punch_out_30)})
    assert r4.status_code == 200, r4.text
    assert r4.json()["overtime_minutes"] == 30


def test_overnight_shift_date_is_previous_day_when_required(client):
    create_employee(client, "EMP0006", shift_start="22:00", shift_end="06:00", joined_on="2026-01-05")
    punch_in = datetime(2026, 1, 5, 19, 30, 0, tzinfo=timezone.utc)
    r1 = client.post("/attendance/punch-in", json={"emp_code": "EMP0006", "punched_at": to_ms(punch_in)})
    assert r1.status_code == 201, r1.text
    assert r1.json()["date"] == "2026-01-05"
    assert r1.json()["late_minutes"] == 180


def test_overnight_punch_out_uses_next_day_shift_end(client):
    create_employee(client, "EMP0017", shift_start="22:00", shift_end="06:00", joined_on="2026-01-05")
    punch_in = datetime(2026, 1, 5, 16, 25, tzinfo=timezone.utc)
    created = client.post("/attendance/punch-in", json={"emp_code": "EMP0017", "punched_at": to_ms(punch_in)})
    assert created.status_code == 201 and created.json()["date"] == "2026-01-05"
    punch_out = datetime(2026, 1, 6, 1, 10, tzinfo=timezone.utc)
    closed = client.post("/attendance/punch-out", json={"emp_code": "EMP0017", "punched_at": to_ms(punch_out)})
    assert closed.status_code == 200, closed.text
    assert closed.json()["work_hours"] == 8.75
    assert closed.json()["overtime_minutes"] == 40


def test_employee_monthly_treats_mid_month_joiners_and_empty_logs_correctly(real_mongo_client):
    create_employee(real_mongo_client, "EMP0007", shift_start="09:30", shift_end="18:30", joined_on="2026-01-15")
    response = real_mongo_client.get("/analytics/employees/EMP0007/monthly", params={"month": "2026-01"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["working_days"] == 12
    assert data["present_days"] == 0.0
    assert data["attendance_pct"] == 0.0


def test_punch_timestamps_are_truncated_to_whole_seconds(client):
    create_employee(client, "EMP0008", shift_start="09:30", shift_end="18:30", joined_on="2026-01-05")
    punch_in = datetime(2026, 1, 5, 4, 10, 0, 900000, tzinfo=timezone.utc)
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0008", "punched_at": to_ms(punch_in)})
    assert response.status_code == 201, response.text
    assert response.json()["punch_in"] == to_ms(datetime(2026, 1, 5, 4, 10, 0, tzinfo=timezone.utc))
    assert response.json()["late_minutes"] == 0


def test_regularizing_absent_record_does_not_require_unset_punch_datetimes(client):
    create_employee(client, "EMP0009")
    main.db.attendance_logs.insert_one({
        "emp_code": "EMP0009",
        "date": "2026-01-05",
        "status": "ABSENT",
        "punch_in": None,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": 0,
        "overtime_minutes": 0,
        "half_day": False,
    })
    response = client.patch(
        "/attendance/EMP0009/2026-01-05",
        json={"status": "LEAVE", "reason": "Approved leave", "regularized_by": "manager"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "LEAVE"


def test_simultaneous_duplicate_employee_and_punch_in_requests(client):
    payload = {
        "emp_code": "EMP0010",
        "name": "Employee EMP0010",
        "email": "emp0010@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05",
    }
    with ThreadPoolExecutor(max_workers=8) as pool:
        employee_responses = list(pool.map(lambda _: client.post("/employees", json=payload), range(8)))
    assert sorted(response.status_code for response in employee_responses) == [201] + [409] * 7

    punch_time = to_ms(datetime(2026, 1, 5, 4, 0, 0, tzinfo=timezone.utc))
    with ThreadPoolExecutor(max_workers=8) as pool:
        punch_responses = list(pool.map(
            lambda _: client.post("/attendance/punch-in", json={"emp_code": "EMP0010", "punched_at": punch_time}),
            range(8),
        ))
    assert sorted(response.status_code for response in punch_responses) == [201] + [409] * 7


def test_simultaneous_punch_out_requests_have_one_winner(client):
    create_employee(client, "EMP0011", joined_on="2026-01-05")
    punch_time = datetime(2026, 1, 5, 4, 0, 0, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0011", "punched_at": to_ms(punch_time)})
    out_time = to_ms(datetime(2026, 1, 5, 13, 0, 0, tzinfo=timezone.utc))
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda _: client.post("/attendance/punch-out", json={"emp_code": "EMP0011", "punched_at": out_time}),
            range(2),
        ))
    assert sorted(response.status_code for response in responses) == [200, 409]


def test_concurrent_regularizations_preserve_successful_audit_entries(client):
    create_employee(client, "EMP0015", joined_on="2026-01-05")
    start = datetime(2026, 1, 5, 4, 0, tzinfo=timezone.utc)
    created = client.post("/attendance/punch-in", json={"emp_code": "EMP0015", "punched_at": to_ms(start)})
    assert created.status_code == 201, created.text

    requests = [
        {"punch_in": to_ms(start + timedelta(minutes=1)), "reason": "Correction A", "regularized_by": "manager-a"},
        {"punch_in": to_ms(start + timedelta(minutes=2)), "reason": "Correction B", "regularized_by": "manager-b"},
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(
            lambda body: client.patch("/attendance/EMP0015/2026-01-05", json=body),
            requests,
        ))
    successes = sum(response.status_code == 200 for response in responses)
    conflicts = sum(response.status_code == 409 for response in responses)
    assert successes + conflicts == 2
    record = main.db.attendance_logs.find_one({"emp_code": "EMP0015", "date": "2026-01-05"})
    assert len(record["history"]) == successes
    expected_authors = {
        requests[index]["regularized_by"]
        for index, response in enumerate(responses)
        if response.status_code == 200
    }
    assert {entry["by"] for entry in record["history"]} == expected_authors


def test_punch_out_does_not_skip_newer_closed_record(client):
    create_employee(client, "EMP0012", joined_on="2026-01-05")
    main.db.attendance_logs.insert_many([
        {"emp_code": "EMP0012", "date": "2026-01-05", "status": "PRESENT", "punch_in": datetime(2026, 1, 5, 4, 0), "punch_out": None},
        {"emp_code": "EMP0012", "date": "2026-01-06", "status": "PRESENT", "punch_in": datetime(2026, 1, 6, 4, 0), "punch_out": datetime(2026, 1, 6, 13, 0)},
    ])
    response = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0012",
        "punched_at": to_ms(datetime(2026, 1, 6, 14, 0, tzinfo=timezone.utc)),
    })
    assert response.status_code == 409


def test_attendance_date_changes_at_ist_midnight(client):
    create_employee(client, "EMP0016", shift_start="09:30", shift_end="18:30", joined_on="2026-01-05")
    before_midnight = datetime(2026, 1, 5, 18, 29, 59, 900000, tzinfo=timezone.utc)
    after_midnight = datetime(2026, 1, 5, 18, 30, 0, 100000, tzinfo=timezone.utc)
    first = client.post("/attendance/punch-in", json={"emp_code": "EMP0016", "punched_at": to_ms(before_midnight)})
    second = client.post("/attendance/punch-in", json={"emp_code": "EMP0016", "punched_at": to_ms(after_midnight)})
    assert first.status_code == 201 and first.json()["date"] == "2026-01-05"
    assert second.status_code == 201 and second.json()["date"] == "2026-01-06"


def test_half_day_uses_rounded_hours_threshold(client):
    create_employee(client, "EMP0013", shift_start="09:00", shift_end="18:00", joined_on="2026-01-05")
    start = datetime(2026, 1, 5, 3, 30, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": "EMP0013", "punched_at": to_ms(start)})
    exact_450 = client.post("/attendance/punch-out", json={
        "emp_code": "EMP0013",
        "punched_at": to_ms(start + timedelta(hours=4, minutes=30)),
    })
    assert exact_450.status_code == 200, exact_450.text
    assert exact_450.json()["work_hours"] == 4.5
    assert exact_450.json()["half_day"] is False


@pytest.mark.parametrize(
    ("seconds_worked", "expected_hours", "expected_half_day"),
    [(4 * 3600 + 29 * 60 + 41, 4.49, True), (4 * 3600 + 29 * 60 + 42, 4.5, False)],
)
def test_work_hours_round_half_up_before_half_day_comparison(client, seconds_worked, expected_hours, expected_half_day):
    emp_code = "EMP0018" if seconds_worked % 2 else "EMP0019"
    create_employee(client, emp_code, shift_start="09:00", shift_end="18:00", joined_on="2026-01-05")
    start = datetime(2026, 1, 5, 3, 30, tzinfo=timezone.utc)
    client.post("/attendance/punch-in", json={"emp_code": emp_code, "punched_at": to_ms(start)})
    response = client.post("/attendance/punch-out", json={
        "emp_code": emp_code,
        "punched_at": to_ms(start + timedelta(seconds=seconds_worked)),
    })
    assert response.status_code == 200, response.text
    assert response.json()["work_hours"] == expected_hours
    assert response.json()["half_day"] is expected_half_day


@pytest.mark.parametrize("punched_at", [1767586200, 1767586200000.0, "1767586200000", True, None])
def test_invalid_punch_timestamp_forms_return_422(client, punched_at):
    create_employee(client, "EMP0014")
    response = client.post("/attendance/punch-in", json={"emp_code": "EMP0014", "punched_at": punched_at})
    assert response.status_code == 422


def test_punch_out_defaults_to_current_time_and_ignores_client_derived_fields(client):
    create_employee(client, "EMP0020", joined_on="2026-01-05")
    past_punch = datetime.now(timezone.utc) - timedelta(hours=1)
    punch_in = client.post("/attendance/punch-in", json={
        "emp_code": "EMP0020", "punched_at": to_ms(past_punch), "late_minutes": 999,
    })
    assert punch_in.status_code == 201, punch_in.text
    assert punch_in.json()["late_minutes"] != 999
    punch_out = client.post("/attendance/punch-out", json={"emp_code": "EMP0020"})
    assert punch_out.status_code == 200, punch_out.text
    assert punch_out.json()["punch_out"] is not None


def test_invalid_calendar_dates_and_status_filters_return_422(client):
    bad_employee = {
        "emp_code": "EMP0021", "name": "Bad Date", "email": "bad@example.com",
        "department": "Ops", "joined_on": "2026-02-30",
    }
    assert client.post("/employees", json=bad_employee).status_code == 422
    assert client.get("/attendance", params={"date_from": "2026-02-30"}).status_code == 422
    assert client.get("/attendance", params={"status": "INVALID"}).status_code == 422


def test_missing_resources_and_noop_regularization_use_contract_errors(client):
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP9999"}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-01-05", json={
        "status": "LEAVE", "reason": "Approved leave", "regularized_by": "manager",
    }).status_code == 404
    assert client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-01"}).status_code == 404

    create_employee(client, "EMP0022")
    main.db.attendance_logs.insert_one({
        "emp_code": "EMP0022", "date": "2026-01-05", "status": "ABSENT",
        "punch_in": None, "punch_out": None, "work_hours": None,
        "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
    })
    no_op = client.patch("/attendance/EMP0022/2026-01-05", json={
        "reason": "No change", "regularized_by": "manager",
    })
    assert no_op.status_code == 422
