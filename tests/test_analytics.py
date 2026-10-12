from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import mongomock
import pytest
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

import app.main as main


@pytest.fixture

def client():
    main.client = mongomock.MongoClient()
    main.db = main.client["analytics_db"]
    main.ensure_indexes()
    with TestClient(main.app) as test_client:
        yield test_client


def employee(emp_code, department="Ops", joined_on="2026-01-01"):
    main.db.employees.insert_one({
        "emp_code": emp_code,
        "name": f"Name {emp_code}",
        "email": f"{emp_code.lower()}@example.com",
        "department": department,
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": joined_on,
        "created_at": datetime(2026, 1, 1),
    })


def attendance(emp_code, day, status="PRESENT", work_hours=None, late_minutes=0, half_day=False, overtime=0):
    main.db.attendance_logs.insert_one({
        "emp_code": emp_code,
        "date": day,
        "status": status,
        "punch_in": None,
        "punch_out": None,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime,
        "half_day": half_day,
        "history": [],
    })


def test_department_summary_uses_record_weighting_and_hired_headcount(real_mongo_client):
    employee("EMP1001", joined_on="2026-01-01")
    employee("EMP1002", joined_on="2026-01-15")
    employee("EMP1003", joined_on="2026-02-01")
    attendance("EMP1001", "2026-01-05", work_hours=8.0, late_minutes=5)
    attendance("EMP1001", "2026-01-10", work_hours=4.0, late_minutes=10, half_day=True)
    attendance("EMP1002", "2026-01-06", status="LEAVE", work_hours=99.0)
    attendance("EMP1002", "2026-01-07", status="ON_DUTY", work_hours=6.0, late_minutes=0)

    response = real_mongo_client.get("/analytics/departments/summary", params={"month": "2026-01", "department": "Ops"})
    assert response.status_code == 200, response.text
    row = response.json()["items"][0]
    assert row["headcount"] == 2
    assert row["present_days"] == 2.0
    assert row["avg_work_hours"] == 6.0
    assert row["late_count"] == 2
    assert row["total_late_minutes"] == 15
    assert row["leave_count"] == 1
    assert row["on_duty_count"] == 1


def test_leaderboard_keeps_ties_crossing_limit(real_mongo_client):
    for code in ("EMP1101", "EMP1102", "EMP1103", "EMP1104"):
        employee(code)
    attendance("EMP1101", "2026-01-05", late_minutes=50)
    attendance("EMP1102", "2026-01-05", late_minutes=40)
    attendance("EMP1103", "2026-01-05", late_minutes=40)
    attendance("EMP1104", "2026-01-05", late_minutes=20)

    response = real_mongo_client.get("/analytics/leaderboard/late", params={"month": "2026-01", "limit": 2})
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert [(row["emp_code"], row["rank"]) for row in rows] == [
        ("EMP1101", 1),
        ("EMP1102", 2),
        ("EMP1103", 2),
    ]


def test_trend_moving_average_uses_seven_calendar_rows(real_mongo_client):
    employee("EMP1201", department="Research", joined_on="2026-01-05")
    attendance("EMP1201", "2026-01-05")
    attendance("EMP1201", "2026-01-12")

    response = real_mongo_client.get(
        "/analytics/departments/Research/trend",
        params={"from": "2026-01-05", "to": "2026-01-12"},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert len(rows) == 8
    assert rows[5]["date"] == "2026-01-10" and rows[5]["attendance_rate"] is None
    assert rows[7]["date"] == "2026-01-12"
    assert rows[7]["attendance_rate"] == 1.0
    assert rows[7]["moving_avg_7d"] == 0.2


def test_trend_rate_is_null_before_department_has_headcount(real_mongo_client):
    employee("EMP1202", department="Research", joined_on="2026-01-08")
    response = real_mongo_client.get(
        "/analytics/departments/Research/trend",
        params={"from": "2026-01-05", "to": "2026-01-07"},
    )
    assert response.status_code == 200, response.text
    rows = response.json()["items"]
    assert all(row["headcount"] == 0 for row in rows)
    assert all(row["attendance_rate"] is None for row in rows)
    assert all(row["moving_avg_7d"] is None for row in rows)


def test_real_mongo_indexes_and_execution_stats(real_mongo_client):
    employee("EMP1301", department="PlanCheck")
    attendance("EMP1301", "2026-01-05", late_minutes=5)
    employee_indexes = main.db.employees.index_information()
    attendance_indexes = main.db.attendance_logs.index_information()
    assert any(index.get("unique") and index["key"] == [("emp_code", 1)] for index in employee_indexes.values())
    assert any(index.get("unique") and index["key"] == [("emp_code", 1), ("date", 1)] for index in attendance_indexes.values())
    with pytest.raises(DuplicateKeyError):
        main.db.employees.insert_one({
            "emp_code": "EMP1301", "name": "Duplicate", "email": "duplicate@example.com",
            "department": "PlanCheck", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01",
        })

    response = real_mongo_client.get("/admin/explain/attendance_list", params={"emp_code": "EMP1301"})
    assert response.status_code == 200, response.text
    explain = response.json()["explain"]
    assert "executionStats" in explain
    winning_plan = explain["queryPlanner"]["winningPlan"]
    execution_stages = explain["executionStats"]["executionStages"]

    def stages(value):
        found = []
        if isinstance(value, dict):
            if isinstance(value.get("stage"), str):
                found.append(value["stage"])
            for child in value.values():
                found.extend(stages(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(stages(child))
        return found

    used_stages = stages(winning_plan) + stages(execution_stages)
    assert "IXSCAN" in used_stages
    assert "COLLSCAN" not in used_stages


def test_real_mongo_concurrent_attendance_updates(real_mongo_client):
    employee("EMP1302", department="Concurrency")
    create_responses = None
    employee_payload = {
        "emp_code": "EMP1303", "name": "Concurrent", "email": "concurrent@example.com",
        "department": "Concurrency", "joined_on": "2026-01-05",
    }
    with ThreadPoolExecutor(max_workers=6) as pool:
        create_responses = list(pool.map(lambda _: real_mongo_client.post("/employees", json=employee_payload), range(6)))
    assert sorted(response.status_code for response in create_responses) == [201] + [409] * 5

    punch_time = 1767585600000
    with ThreadPoolExecutor(max_workers=6) as pool:
        punch_responses = list(pool.map(
            lambda _: real_mongo_client.post("/attendance/punch-in", json={"emp_code": "EMP1303", "punched_at": punch_time}),
            range(6),
        ))
    assert sorted(response.status_code for response in punch_responses) == [201] + [409] * 5

    out_time = 1767618000000
    with ThreadPoolExecutor(max_workers=2) as pool:
        out_responses = list(pool.map(
            lambda _: real_mongo_client.post("/attendance/punch-out", json={"emp_code": "EMP1303", "punched_at": out_time}),
            range(2),
        ))
    assert sorted(response.status_code for response in out_responses) == [200, 409]

    correction_times = [1767585660000, 1767585720000]
    with ThreadPoolExecutor(max_workers=2) as pool:
        corrections = list(pool.map(
            lambda correction: real_mongo_client.patch("/attendance/EMP1303/2026-01-05", json={
                "punch_in": correction,
                "reason": "Concurrency correction",
                "regularized_by": str(correction),
            }),
            correction_times,
        ))
    assert all(response.status_code in {200, 409} for response in corrections)
    record = main.db.attendance_logs.find_one({"emp_code": "EMP1303", "date": "2026-01-05"})
    assert len(record["history"]) == sum(response.status_code == 200 for response in corrections)
