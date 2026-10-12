"""Employee Attendance & Analytics API.

Run with:
    uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import json
import math
import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://localhost:27017"
MONGO_DB = os.getenv("MONGO_DB") or "attendance_db"

client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
STATUS_VALUES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", 1)], unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1), ("emp_code", 1)])
    db.employees.create_index([("joined_on", 1), ("department", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", 1)])
    db.attendance_logs.create_index([("status", 1), ("date", 1), ("emp_code", 1)])
    db.attendance_logs.create_index([("date", 1), ("status", 1), ("late_minutes", 1)])


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


@app.get("/health")
def health() -> dict[str, str]:
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception as exc:  # pragma: no cover - exercised by the runner against a live DB
        raise HTTPException(status_code=503, detail=f"MongoDB unavailable: {exc}") from exc


def round_half_up(value: Optional[Union[float, int, Decimal]], digits: int = 2) -> Optional[float]:
    if value is None:
        return None
    quant = Decimal("1") if digits == 0 else Decimal("1").scaleb(-digits)
    return float((Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP)))


def utc_to_epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    dt = normalize_utc(value)
    return int(dt.astimezone(UTC).timestamp() * 1000)


def epoch_ms_to_utc(value: Optional[int]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromtimestamp((value // 1000), tz=UTC)


def normalize_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_ist(dt: datetime) -> datetime:
    dt = normalize_utc(dt)
    return dt.astimezone(IST)


def iso_date_to_date(value: str) -> date:
    return date.fromisoformat(value)


def validated_iso_date(value: str, field_name: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise HTTPException(status_code=422, detail=f"{field_name} must be a valid YYYY-MM-DD date")
    return parsed


def _normalize_history_changes(changes: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in changes.items():
        if not isinstance(value, dict):
            normalized[key] = value
            continue
        from_value = value.get("from")
        to_value = value.get("to")
        if key in {"punch_in", "punch_out"}:
            normalized[key] = {
                "from": utc_to_epoch_ms(from_value) if from_value is not None else None,
                "to": utc_to_epoch_ms(to_value) if to_value is not None else None,
            }
        else:
            normalized[key] = {"from": from_value, "to": to_value}
    return normalized


def serialize_history(record_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for entry in record_history or []:
        entries.append(
            {
                "at": utc_to_epoch_ms(entry.get("at")),
                "by": entry.get("by"),
                "reason": entry.get("reason"),
                "changes": _normalize_history_changes(entry.get("changes", {}) or {}),
            }
        )
    return entries


def serialize_record(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": serialize_history(doc.get("history", []) or []),
    }


def serialize_employee(doc: dict[str, Any]) -> dict[str, Any]:
    payload = {key: doc[key] for key in ["emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on"]}
    payload["created_at"] = utc_to_epoch_ms(doc.get("created_at"))
    return payload


def date_to_ist(date_str: str) -> datetime:
    return datetime.fromisoformat(date_str + "T00:00:00").replace(tzinfo=IST)


def parse_epoch_millis(value: Optional[int], field_name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer epoch millisecond value")
    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError(f"{field_name} is outside the allowed epoch range")
    return value


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, shift_end: str) -> int:
    punch_in_ist = to_ist(punch_in_utc)
    hour, minute = map(int, shift_start.split(":"))
    start_date = punch_in_ist.date()
    if shift_end <= shift_start and punch_in_ist.strftime("%H:%M") < shift_end:
        start_date -= timedelta(days=1)
    start_ist = datetime.combine(start_date, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=IST)
    delta_seconds = (punch_in_ist - start_ist).total_seconds()
    if delta_seconds <= 600:
        return 0
    return int(math.floor(delta_seconds / 60))


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(seconds / 3600, 2) or 0.0


def compute_overtime_minutes(punch_out_utc: datetime, shift_start: str, shift_end: str, record_date: str) -> int:
    shift_end_date = iso_date_to_date(record_date)
    if shift_end <= shift_start:
        shift_end_date += timedelta(days=1)
    shift_end_dt = datetime.combine(shift_end_date, datetime.strptime(shift_end, "%H:%M").time(), IST)
    delta_seconds = (to_ist(punch_out_utc) - shift_end_dt).total_seconds()
    if delta_seconds <= 0:
        return 0
    minutes = int(math.floor(delta_seconds / 60))
    return minutes if minutes >= 30 else 0


def determine_attendance_date(emp_shift_start: str, emp_shift_end: str, punch_in_utc: datetime) -> str:
    punch_in_ist = to_ist(punch_in_utc)
    if emp_shift_end <= emp_shift_start:
        end_time = datetime.strptime(emp_shift_end, "%H:%M").time()
        if punch_in_ist.time() < end_time:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def month_bounds(month: str) -> tuple[str, str]:
    year, month_num = month.split("-")
    start = date(int(year), int(month_num), 1)
    end = date(int(year), int(month_num), calendar.monthrange(int(year), int(month_num))[1])
    return start.isoformat(), end.isoformat()


def validated_month_bounds(month: str) -> tuple[str, str]:
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) is None:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    try:
        return month_bounds(month)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be a valid YYYY-MM month") from exc


def weekday_count(start: date, end: date, joined_on: str) -> int:
    start_day = max(start, iso_date_to_date(joined_on))
    weekday_count_value = 0
    current = start_day
    while current <= end:
        if current.weekday() < 5:
            weekday_count_value += 1
        current += timedelta(days=1)
    return weekday_count_value


def explain_find(collection: str, query: dict[str, Any], sort: dict[str, int], skip: int, limit: int) -> dict[str, Any]:
    command: dict[str, Any] = {"find": collection, "filter": query, "sort": sort}
    if skip:
        command["skip"] = skip
    command["limit"] = limit
    return db.command("explain", command, verbosity="executionStats")


def explain_aggregate(collection: str, pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def explain_database_aggregate(pipeline: list[dict[str, Any]]) -> dict[str, Any]:
    return db.command(
        "explain",
        {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


def json_safe(value: Any) -> Any:
    return json.loads(json_util.dumps(value))


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_order(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        try:
            parsed_joined_on = date.fromisoformat(self.joined_on)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed_joined_on.isoformat() != self.joined_on:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None)
    status: str = Field(default="PRESENT")

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if value not in PRESENCE_STATUSES:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return value

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return value
        if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at is outside the allowed epoch range")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: Any) -> Any:
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be omitted or epoch milliseconds")
        return value


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None

    @field_validator("punched_at")
    @classmethod
    def validate_epoch(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return value
        if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punched_at is outside the allowed epoch range")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_timestamp(cls, value: Any) -> Any:
        if isinstance(value, dict) and "punched_at" in value and value["punched_at"] is None:
            raise ValueError("punched_at must be omitted or epoch milliseconds")
        return value


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if value not in STATUS_VALUES:
            raise ValueError("status must be one of the allowed attendance statuses")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def validate_epoch(cls, value: Optional[StrictInt]) -> Optional[StrictInt]:
        if value is None:
            return None
        if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
            raise ValueError("punch time is outside the allowed epoch range")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_null_updates(cls, value: Any) -> Any:
        if isinstance(value, dict) and any(key in value and value[key] is None for key in ("status", "punch_in", "punch_out")):
            raise ValueError("status and punch times must be omitted or valid values")
        return value


class EmployeeMonthlySummaryRequest:
    pass


@app.post("/employees", status_code=201)
def create_employee(payload: EmployeeCreate) -> dict[str, Any]:
    if db.employees.find_one({"emp_code": payload.emp_code}):
        raise HTTPException(status_code=409, detail="emp_code already exists")
    document = payload.model_dump()
    document["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(document)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(document)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if department is not None:
        query["department"] = department
    total = db.employees.count_documents(query)
    cursor = db.employees.find(query, {"_id": 0}).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(payload: PunchInRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": payload.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at_ms = payload.punched_at or int(datetime.now(UTC).timestamp() * 1000)
    punched_at = epoch_ms_to_utc(punched_at_ms)
    if punched_at is None:
        raise HTTPException(status_code=422, detail="punched_at is required")

    attendance_date = determine_attendance_date(employee["shift_start"], employee["shift_end"], punched_at)
    if db.attendance_logs.find_one({"emp_code": payload.emp_code, "date": attendance_date}):
        raise HTTPException(status_code=409, detail="already punched in for that date")

    punch_in_utc = punched_at.astimezone(UTC)
    doc = {
        "emp_code": payload.emp_code,
        "date": attendance_date,
        "status": payload.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_utc, employee["shift_start"], employee["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="already punched in for that date") from exc
    return serialize_record(doc)


@app.post("/attendance/punch-out", status_code=200)
def punch_out(payload: PunchOutRequest) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": payload.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_out_at = epoch_ms_to_utc(payload.punched_at or int(datetime.now(UTC).timestamp() * 1000))
    if punched_out_at is None:
        raise HTTPException(status_code=422, detail="punched_at is invalid")

    punched_out_store_dt = normalize_utc(punched_out_at).replace(tzinfo=None)

    current = db.attendance_logs.find_one(
        {"emp_code": payload.emp_code, "punch_in": {"$lte": punched_out_store_dt}},
        sort=[("punch_in", -1)],
    )
    if current is None:
        raise HTTPException(status_code=404, detail="no punch-in found")
    if current.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="that record is already punched out")

    punch_in_utc = normalize_utc(current["punch_in"])
    if punch_in_utc is None:
        raise HTTPException(status_code=422, detail="punch_in is invalid")
    if punched_out_at <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if (punched_out_at - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punched_out_at)
    shift_end = employee["shift_end"]
    record_date = current["date"]
    overtime_minutes = compute_overtime_minutes(punched_out_at, employee["shift_start"], shift_end, record_date)

    rounded_work_hours = round_half_up(work_hours, 2)
    half_day = bool(rounded_work_hours is not None and rounded_work_hours < 4.5)

    update = {
        "$set": {
            "punch_out": punched_out_at,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }
    }
    result = db.attendance_logs.update_one(
        {"_id": current["_id"], "punch_out": None, "punch_in": current["punch_in"]},
        update,
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="that record is already punched out")
    refreshed = db.attendance_logs.find_one({"_id": current["_id"]})
    return serialize_record(refreshed)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        if date_from is not None:
            validated_iso_date(date_from, "date_from")
        if date_to is not None:
            validated_iso_date(date_to, "date_to")
        date_range: dict[str, str] = {}
        if date_from is not None:
            date_range["$gte"] = date_from
        if date_to is not None:
            date_range["$lte"] = date_to
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        query["date"] = date_range
    if status is not None:
        query["status"] = status
    total = db.attendance_logs.count_documents(query)
    cursor = db.attendance_logs.find(query).sort([("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    items = [serialize_record(doc) for doc in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, payload: RegularizeRequest) -> dict[str, Any]:
    validated_iso_date(date, "date")
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    current_status = record.get("status")
    current_punch_in = normalize_utc(record.get("punch_in"))
    current_punch_out = normalize_utc(record.get("punch_out"))
    final_status = payload.status if payload.status is not None else current_status
    final_punch_in = payload.punch_in if payload.punch_in is not None else (None if current_punch_in is None else int(current_punch_in.timestamp() * 1000))
    final_punch_out = payload.punch_out if payload.punch_out is not None else (None if current_punch_out is None else int(current_punch_out.timestamp() * 1000))

    if final_status in {"ABSENT", "LEAVE"} and (payload.punch_in is not None or payload.punch_out is not None):
        raise HTTPException(status_code=422, detail="ABSENT and LEAVE records must not include punch times")

    if final_status in PRESENCE_STATUSES and final_punch_in is None:
        raise HTTPException(status_code=422, detail="presence status requires a punch_in")

    if final_punch_in is not None:
        final_punch_in_dt = epoch_ms_to_utc(final_punch_in)
        if final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="punch_in is invalid")
        attendance_date = determine_attendance_date(employee["shift_start"], employee["shift_end"], final_punch_in_dt)
        if attendance_date != date:
            raise HTTPException(status_code=422, detail="punch_in must fall on the attendance date")

    if final_punch_in is not None and final_punch_out is not None:
        final_punch_in_dt = epoch_ms_to_utc(final_punch_in)
        final_punch_out_dt = epoch_ms_to_utc(final_punch_out)
        if final_punch_out_dt is None or final_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out must be valid instants")
        if final_punch_out_dt <= final_punch_in_dt:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if (final_punch_out_dt - final_punch_in_dt).total_seconds() > 86400:
            raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    original_changes = {
        "status": record.get("status"),
        "punch_in": current_punch_in,
        "punch_out": current_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    next_status = final_status
    next_punch_in = final_punch_in_dt if final_punch_in is not None else None
    next_punch_out = final_punch_out_dt if final_punch_out is not None else None
    next_late_minutes = 0
    next_overtime = 0
    next_half_day = False
    next_work_hours = None

    if next_status in PRESENCE_STATUSES and next_punch_in is not None:
        next_late_minutes = compute_late_minutes(next_punch_in, employee["shift_start"], employee["shift_end"])
        if next_punch_out is not None:
            next_work_hours = compute_work_hours(next_punch_in, next_punch_out)
            next_overtime = compute_overtime_minutes(next_punch_out, employee["shift_start"], employee["shift_end"], date)
            rounded_hours = round_half_up(next_work_hours, 2)
            next_half_day = bool(rounded_hours is not None and rounded_hours < 4.5)

    if next_status in {"ABSENT", "LEAVE"}:
        next_punch_in = None
        next_punch_out = None
        next_work_hours = None
        next_late_minutes = 0
        next_overtime = 0
        next_half_day = False

    if next_status == current_status and next_punch_in == current_punch_in and next_punch_out == current_punch_out and next_work_hours == record.get("work_hours") and next_late_minutes == record.get("late_minutes", 0) and next_overtime == record.get("overtime_minutes", 0) and next_half_day == record.get("half_day", False):
        raise HTTPException(status_code=422, detail="request changes nothing")

    update = {
        "status": next_status,
        "punch_in": next_punch_in,
        "punch_out": next_punch_out,
        "work_hours": next_work_hours,
        "late_minutes": next_late_minutes,
        "overtime_minutes": next_overtime,
        "half_day": next_half_day,
    }
    changes: dict[str, Any] = {}
    for field, new_value in update.items():
        prior_value = original_changes.get(field)
        if field in {"punch_in", "punch_out"}:
            if prior_value != new_value:
                changes[field] = {"from": prior_value, "to": new_value}
        elif prior_value != new_value:
            changes[field] = {"from": prior_value, "to": new_value}

    if not changes:
        raise HTTPException(status_code=422, detail="request changes nothing")

    if payload.status is None and payload.punch_in is None and payload.punch_out is None:
        raise HTTPException(status_code=422, detail="request changes nothing")

    entry = {
        "at": datetime.now(UTC),
        "by": payload.regularized_by,
        "reason": payload.reason,
        "changes": changes,
    }
    history_filter = {"history": record["history"]} if "history" in record else {"history": {"$exists": False}}
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record.get("status"),
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            **history_filter,
        },
        {"$set": update, "$push": {"history": entry}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=409, detail="attendance record changed during regularization")
    refreshed = db.attendance_logs.find_one({"_id": record["_id"]})
    return serialize_record(refreshed)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    start_iso, end_iso = validated_month_bounds(month)
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_iso, "$lte": end_iso}}},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]
    row = next(db.attendance_logs.aggregate(pipeline), {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0})

    working_days = weekday_count(iso_date_to_date(start_iso), iso_date_to_date(end_iso), employee["joined_on"])
    present_days = float(row.get("present_days", 0) or 0)
    attendance_pct = None if working_days == 0 else round_half_up((present_days / working_days) * 100, 4)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(row.get("present_days", 0) or 0),
        "leave_days": int(row.get("leave_days", 0) or 0),
        "late_count": int(row.get("late_count", 0) or 0),
        "total_late_minutes": int(row.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(row.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
) -> dict[str, Any]:
    start_iso, end_iso = validated_month_bounds(month)
    employee_query: dict[str, Any] = {"joined_on": {"$lte": end_iso}}
    if department is not None:
        employee_query["department"] = department
    employee_cursor = db.employees.aggregate([
        {"$match": employee_query},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ])
    department_headcounts = {row["_id"]: row["headcount"] for row in employee_cursor}

    attendance_pipeline = [
        {"$match": {"date": {"$gte": start_iso, "$lte": end_iso}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
        {"$match": {"employee.joined_on": {"$lte": end_iso}}},
        {"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$work_hours", None]},
                ]},
                "$work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, "$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    if department is not None:
        attendance_pipeline.insert(3, {"$match": {"employee.department": department}})
    attendance_rows = {row["_id"]: row for row in db.attendance_logs.aggregate(attendance_pipeline)}

    items = []
    for department_name in sorted(department_headcounts):
        headcount = department_headcounts[department_name]
        if headcount == 0:
            continue
        stats = attendance_rows.get(department_name, {})
        avg_work_hours = round_half_up(stats.get("avg_work_hours"), 2)
        items.append(
            {
                "department": department_name,
                "headcount": headcount,
                "present_days": float(stats.get("present_days", 0) or 0),
                "avg_work_hours": avg_work_hours,
                "late_count": int(stats.get("late_count", 0) or 0),
                "total_late_minutes": int(stats.get("total_late_minutes", 0) or 0),
                "leave_count": int(stats.get("leave_count", 0) or 0),
                "on_duty_count": int(stats.get("on_duty_count", 0) or 0),
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
) -> dict[str, Any]:
    start_iso, end_iso = validated_month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start_iso, "$lte": end_iso}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0,
            "rank": 1,
            "emp_code": "$_id",
            "name": 1,
            "department": 1,
            "total_late_minutes": 1,
            "late_count": 1,
        }},
    ])
    rows = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
) -> dict[str, Any]:
    validated_iso_date(from_date, "from")
    validated_iso_date(to_date, "to")
    from_iso = iso_date_to_date(from_date)
    to_iso = iso_date_to_date(to_date)
    if to_iso < from_iso:
        raise HTTPException(status_code=422, detail="to must be on or after from")
    if (to_iso - from_iso).days + 1 > 92:
        raise HTTPException(status_code=422, detail="trend range must not exceed 92 days")

    employee_doc = db.employees.find_one({"department": department})
    if employee_doc is None:
        raise HTTPException(status_code=404, detail="department not found")

    trend_pipeline = [
        {"$documents": [{
            "start_date": datetime.combine(from_iso, datetime.min.time(), UTC),
            "days_count": (to_iso - from_iso).days + 1,
        }]},
        {"$project": {
            "dates": {"$map": {
                "input": {"$range": [0, "$days_count"]},
                "as": "offset",
                "in": {"$dateToString": {
                    "format": "%Y-%m-%d",
                    "date": {"$dateAdd": {"startDate": "$start_date", "unit": "day", "amount": "$$offset"}},
                    "timezone": "UTC",
                }},
            }},
        }},
        {"$unwind": "$dates"},
        {"$set": {"date": "$dates"}},
        {"$lookup": {
            "from": "employees",
            "let": {"trend_date": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$trend_date"]}}},
                {"$count": "headcount"},
            ],
            "as": "headcount_result",
        }},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.headcount", 0]}, 0]}}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"trend_date": "$date"},
            "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$trend_date"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                        0,
                    ]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ],
            "as": "attendance_stats",
        }},
        {"$set": {
            "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}, 5]},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_stats.present_count", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_stats.late_count", 0]}, 0]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$trunc": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]},
            None,
        ]}}},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {"moving_avg_unrounded": {
                "$avg": "$attendance_rate",
                "window": {"documents": [-6, 0]},
            }},
        }},
        {"$set": {"moving_avg_7d": {"$cond": [
            {"$eq": ["$moving_avg_unrounded", None]},
            None,
            {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$moving_avg_unrounded", 10000]}, 0.5]}}, 10000]},
        ]}}},
        {"$project": {
            "_id": 0,
            "date": 1,
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]
    return {"department": department, "items": list(db.aggregate(trend_pipeline))}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    valid_endpoints = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in valid_endpoints:
        raise HTTPException(status_code=422, detail="endpoint not supported")

    if month is not None:
        validated_month_bounds(month)

    if endpoint == "attendance_list":
        query: dict[str, Any] = {}
        if emp_code is not None:
            query["emp_code"] = emp_code
        if date_from is not None or date_to is not None:
            date_range: dict[str, str] = {}
            if date_from is not None:
                validated_iso_date(date_from, "date_from")
                date_range["$gte"] = date_from
            if date_to is not None:
                validated_iso_date(date_to, "date_to")
                date_range["$lte"] = date_to
            if date_from is not None and date_to is not None and date_from > date_to:
                raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
            query["date"] = date_range
        if status is not None:
            query["status"] = status
        explain_doc = explain_find("attendance_logs", query, {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size)
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        start_iso, end_iso = validated_month_bounds(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": start_iso, "$lte": end_iso}}},
            {"$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": {"$cond": [
                    {"$and": [
                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                    ]},
                    {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                    0,
                ]}},
                "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }},
        ]
        explain_doc = explain_aggregate("attendance_logs", pipeline)
        collection = "attendance_logs"

    elif endpoint == "department_summary":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start_iso, end_iso = validated_month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": start_iso, "$lte": end_iso}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$match": {"employee.joined_on": {"$lte": end_iso}}},
        ]
        if department is not None:
            pipeline.insert(3, {"$match": {"employee.department": department}})
        pipeline.append({"$group": {
            "_id": "$employee.department",
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
                    {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
                ]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "avg_work_hours": {"$avg": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$work_hours", None]},
                ]},
                "$work_hours",
                None,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, "$late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }})
        explain_doc = explain_aggregate("attendance_logs", pipeline)
        collection = "attendance_logs"

    elif endpoint == "late_leaderboard":
        if month is None:
            raise HTTPException(status_code=422, detail="month is required")
        start_iso, end_iso = validated_month_bounds(month)
        pipeline = [
            {"$match": {"date": {"$gte": start_iso, "$lte": end_iso}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
            {"$unwind": "$employee"},
            {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}}}},
        ]
        if department is not None:
            pipeline.insert(3, {"$match": {"employee.department": department}})
        pipeline[-1] = {"$group": {
            "_id": "$emp_code",
            "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, "$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}},
        }}
        pipeline.extend([
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {"$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }},
        ])
        explain_doc = explain_aggregate("attendance_logs", pipeline)
        collection = "attendance_logs"

    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        validated_iso_date(from_date, "from")
        validated_iso_date(to_date, "to")
        from_day = iso_date_to_date(from_date)
        to_day = iso_date_to_date(to_date)
        if to_day < from_day or (to_day - from_day).days + 1 > 92:
            raise HTTPException(status_code=422, detail="invalid trend date range")
        pipeline = [
            {"$documents": [{"start_date": datetime.combine(from_day, datetime.min.time(), UTC), "days_count": (to_day - from_day).days + 1}]},
            {"$project": {"dates": {"$map": {
                "input": {"$range": [0, "$days_count"]},
                "as": "offset",
                "in": {"$dateToString": {"format": "%Y-%m-%d", "date": {"$dateAdd": {"startDate": "$start_date", "unit": "day", "amount": "$$offset"}}, "timezone": "UTC"}},
            }}}},
            {"$unwind": "$dates"},
            {"$set": {"date": "$dates"}},
            {"$lookup": {"from": "employees", "let": {"trend_date": "$date"}, "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$trend_date"]}}},
                {"$count": "headcount"},
            ], "as": "headcount_result"}},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.headcount", 0]}, 0]}}},
            {"$lookup": {"from": "attendance_logs", "let": {"trend_date": "$date"}, "pipeline": [
                {"$match": {"$expr": {"$eq": ["$date", "$$trend_date"]}}},
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"}},
                {"$unwind": "$employee"},
                {"$match": {"employee.department": department}},
                {"$group": {
                    "_id": None,
                    "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                }},
            ], "as": "attendance_stats"}},
            {"$set": {
                "is_working_day": {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "timezone": "UTC"}}}, 5]},
                "present_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_stats.present_count", 0]}, 0]},
                "late_count": {"$ifNull": [{"$arrayElemAt": ["$attendance_stats.late_count", 0]}, 0]},
            }},
            {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": [{"$trunc": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
            {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_unrounded": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
            {"$set": {"moving_avg_7d": {"$cond": [{"$eq": ["$moving_avg_unrounded", None]}, None, {"$divide": [{"$trunc": {"$add": [{"$multiply": ["$moving_avg_unrounded", 10000]}, 0.5]}}, 10000]}]}}},
            {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
            {"$sort": {"date": 1}},
        ]
        explain_doc = explain_database_aggregate(pipeline)
        collection = "database"

    return {"endpoint": endpoint, "collection": collection, "explain": json_safe(explain_doc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
