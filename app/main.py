"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

All application code lives in this single file. See openapi.yaml for the contract (rules R1-R10) and
DATA_MODEL.md for what is stored in MongoDB.
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo returns timezone-aware UTC datetimes instead of naive ones.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000
MAX_TREND_DAYS = 92

_indexes_ready = False


def ensure_indexes() -> None:
    """Idempotent; called at startup and again from /health if MongoDB was not reachable at startup."""
    global _indexes_ready
    emp, att = db.employees, db.attendance_logs
    # unique business key; also serves lookups by emp_code and the default sort of GET /employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    # GET /employees?department=... sorted by emp_code
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
    # department summary / trend: department + joined_on <= period end
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined_on")
    # department summary without a department filter
    emp.create_index([("joined_on", ASCENDING)], name="joined_on")
    # one record per (employee, day): the race guard for punch-in; also per-employee queries
    att.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique")
    # GET /attendance default sort (date desc, emp_code asc) and month-wide analytics scans by date range
    att.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    # GET /attendance?status=...
    att.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code")
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.exception("could not create indexes at startup; will retry from /health")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (R1)
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return calendar.timegm(dt.utctimetuple()) * 1000 + dt.microsecond // 1000


def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def truncate(dt: datetime) -> datetime:
    return dt.replace(microsecond=0)


def parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date_cls:
    """R1: IST date of the punch-in; an overnight-shift punch-in before shift_end belongs to the previous day."""
    local = punch_in.astimezone(IST)
    if is_overnight(shift_start, shift_end) and local.time() < parse_hhmm(shift_end):
        return local.date() - timedelta(days=1)
    return local.date()


def _at_ist(day: date_cls, hhmm: str) -> datetime:
    return datetime.combine(day, parse_hhmm(hhmm), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, day: date_cls, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift_start; then whole minutes since shift_start."""
    seconds = int((truncate(punch_in) - _at_ist(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600 rounded to 2 decimals half-up (exact decimal arithmetic, no float rounding)."""
    seconds = int((truncate(punch_out) - truncate(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, day: date_cls, shift_start: str, shift_end: str) -> int:
    """R3: whole minutes after shift_end (next day for overnight shifts), only if at least 30."""
    end = _at_ist(day, shift_end)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((truncate(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def is_half_day(work_hours: float) -> bool:
    """R5: judged on the already rounded work_hours."""
    return Decimal(str(work_hours)) < HALF_DAY_HOURS


def half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def to_decimal(value) -> Decimal:
    if isinstance(value, Decimal128):
        return value.to_decimal()
    return Decimal(str(value))


def derived_fields(emp: dict, day: date_cls, status: str, punch_in, punch_out) -> dict:
    """All stored derived values for a record (DATA_MODEL.md, 'For a record your own code writes')."""
    if status not in PRESENCE_STATUSES:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    out = {
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in, day, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
    }
    if punch_out is not None:
        out["work_hours"] = compute_work_hours(punch_in, punch_out)
        out["overtime_minutes"] = compute_overtime(punch_out, day, emp["shift_start"], emp["shift_end"])
        out["half_day"] = is_half_day(out["work_hours"])
    return out


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def bad_request(loc: str, name: str, msg: str) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": [loc, name], "msg": msg, "input": None}])


def parse_date(value: str, name: str, loc: str = "query") -> date_cls:
    try:
        if len(value) != 10 or not value.replace("-", "").isascii() or value[4] != "-" or value[7] != "-":
            raise ValueError
        return date_cls.fromisoformat(value)
    except ValueError:
        raise bad_request(loc, name, "must be a valid date, YYYY-MM-DD")


def month_bounds(month: str):
    """'YYYY-MM' -> (first_day, last_day) as dates."""
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = date_cls(year, mon, 1)
    except (ValueError, IndexError):
        raise bad_request("query", "month", "must be a valid month, YYYY-MM")
    return first, date_cls(year, mon, calendar.monthrange(year, mon)[1])


MonthParam = Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]
PageParam = Annotated[int, Query(ge=1)]
PageSizeParam = Annotated[int, Query(ge=1, le=100)]
LimitParam = Annotated[int, Query(ge=1, le=50)]
StatusName = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=MIN_MS, le=MAX_MS)]
HHMM = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]


def get_employee(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    return emp


def record_out(doc: dict) -> dict:
    """Stored attendance document -> API representation (epoch ms, defaults for legacy gaps, no _id)."""

    def convert(v):
        if isinstance(v, datetime):
            return to_ms(v)
        if isinstance(v, dict):
            return {k: convert(x) for k, x in v.items()}
        if isinstance(v, list):
            return [convert(x) for x in v]
        return v

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": convert(doc.get("punch_in")),
        "punch_out": convert(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": convert(doc.get("history") or []),
    }


def employee_out(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")}
    created = doc.get("created_at")
    out["created_at"] = to_ms(created) if created else None
    return out


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: HHMM = "09:30"
    shift_end: HHMM = "18:30"
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _joined_on_is_date(cls, v: str) -> str:
        try:
            parse_date(v, "joined_on")
        except RequestValidationError:
            raise ValueError("must be a valid date, YYYY-MM-DD")
        return v


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[StatusName] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Health / employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        if not _indexes_ready:
            ensure_indexes()
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if body.shift_start == body.shift_end:
        raise bad_request("body", "shift_end", "shift_end must differ from shift_start")
    doc = body.model_dump()
    doc["created_at"] = utc_now_seconds()
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code is the arbiter, not a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageParam = 1, page_size: PageSizeParam = 20):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    day = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derived_fields(emp, day, body.status, ts, None),
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one of N racing requests wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


def attendance_query(emp_code, date_from, date_to, status, page, page_size):
    """Filter/sort/skip/limit of GET /attendance, shared with the explain endpoint."""
    start = parse_date(date_from, "date_from") if date_from else None
    end = parse_date(date_to, "date_to") if date_to else None
    if start and end and start > end:
        raise bad_request("query", "date_from", "date_from must not be after date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if start or end:
        q["date"] = {}
        if start:
            q["date"]["$gte"] = start.isoformat()
        if end:
            q["date"]["$lte"] = end.isoformat()
    if status:
        q["status"] = status
    sort = [("date", DESCENDING), ("emp_code", ASCENDING)]
    return q, sort, (page - 1) * page_size, page_size


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusName] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    q, sort, skip, limit = attendance_query(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort(sort).skip(skip).limit(limit)
    return {"items": [record_out(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    ts = from_ms(body.punched_at) if body.punched_at is not None else utc_now_seconds()
    # most recent record (by attendance date) whose punch_in is at or before the punch-out; null punch_in never matches
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("date", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = truncate(rec["punch_in"])
    if ts <= pin:
        raise bad_request("body", "punched_at", "punched_at must be after punch_in")
    if ts - pin > timedelta(hours=24):
        raise bad_request("body", "punched_at", "punched_at is more than 24 hours after punch_in")
    day = date_cls.fromisoformat(rec["date"])
    fields = derived_fields(emp, day, rec["status"], pin, ts)
    fields.pop("late_minutes")  # late_minutes is a punch-in fact; it is not touched here
    # the filter makes the transition atomic: only one concurrent request can find punch_out still null
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date", loc="path")
    emp = get_employee(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")
    for name in ("status", "punch_in", "punch_out"):
        if name in body.model_fields_set and getattr(body, name) is None:
            raise bad_request("body", name, "must not be null")

    old_status, old_in, old_out = rec["status"], rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    if new_status in PRESENCE_STATUSES:
        new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
        new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise bad_request("body", "punch_in", "a presence status requires a punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise bad_request("body", "punch_in", "punch_in must stay on the record's attendance date")
        if new_out is not None and not (truncate(new_in) < truncate(new_out) <= truncate(new_in) + timedelta(hours=24)):
            raise bad_request("body", "punch_out", "punch_out must be after punch_in and within 24 hours")
    else:
        if body.punch_in is not None or body.punch_out is not None:
            raise bad_request("body", "status", f"{new_status} clears punch times; do not supply them")
        new_in = new_out = None

    if (new_status, new_in, new_out) == (old_status, old_in, old_out):
        raise bad_request("body", "status", "the request changes nothing")

    new = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived_fields(emp, date_cls.fromisoformat(date), new_status, new_in, new_out),
    }
    old = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    changes = {k: {"from": old[k], "to": v} for k, v in new.items() if v != old[k]}
    entry = {"at": utc_now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: apply only if the record is exactly as we read it (including how many history entries
    # it has). A concurrent correction or punch-out makes this match nothing, and the loser gets 409 instead of
    # silently overwriting the winner's history.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, len(rec.get("history") or [])]},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics pipelines
# --------------------------------------------------------------------------- #
def month_accumulators() -> dict:
    """$group accumulators over the STORED derived values of one employee's (or all) records in a month.

    present_x2 is twice the present days (a half day is 1, a full day 2, Mon-Fri presence records only), so
    everything stays an integer until the final division by two.
    """
    is_present = {"$in": ["$status", PRESENCE_STATUSES]}
    day_of_week = {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "onError": None}}}
    is_weekday = {"$in": [day_of_week, [1, 2, 3, 4, 5]]}
    late = {"$ifNull": ["$late_minutes", 0]}
    has_hours = {"$and": [is_present, {"$in": [{"$type": "$work_hours"}, ["double", "int", "long", "decimal"]]}]}
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    return {
        "present_x2": {"$sum": {"$cond": [{"$and": [is_present, is_weekday]}, {"$cond": [half, 1, 2]}, 0]}},
        "hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},  # exact decimal sum
        "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
        "late_total": {"$sum": {"$cond": [{"$gt": [late, 0]}, late, 0]}},
        "overtime_total": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
    }


def date_range_match(first: date_cls, last: date_cls) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": date_range_match(first, last)}},
        {"$group": {"_id": None, **month_accumulators()}},
    ]


def count_working_days(start: date_cls, end: date_cls) -> int:
    days = (end - start).days + 1
    return sum(1 for i in range(max(days, 0)) if (start + timedelta(days=i)).weekday() < 5)


def avg_hours(row: dict) -> Optional[float]:
    if not row.get("hours_n"):
        return None
    return half_up(to_decimal(row["hours_sum"]) / Decimal(row["hours_n"]), 2)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthParam):
    first, last = month_bounds(month)
    emp = get_employee(emp_code)
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, month)))
    r = rows[0] if rows else {}
    # R7: working days run from joined_on for a mid-month joiner (0 if they join after the month)
    start = max(first, date_cls.fromisoformat(emp["joined_on"]))
    working_days = count_working_days(start, last)
    present = Decimal(r.get("present_x2", 0)) / 2
    pct = half_up(present / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": float(present),
        "leave_days": r.get("leave_count", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("late_total", 0),
        "total_overtime_minutes": r.get("overtime_total", 0),
        "attendance_pct": pct,
    }


def summary_pipeline(month: str, department: Optional[str]) -> list:
    """Starts from employees so people without any log still count (R9), then joins their month's logs."""
    first, last = month_bounds(month)
    emp_match = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        emp_match["department"] = department
    accumulators = month_accumulators()
    return [
        {"$match": emp_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": date_range_match(first, last)}},
                    {"$group": {"_id": None, **accumulators}},
                ],
                "as": "m",
            }
        },
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{k: {"$sum": {"$ifNull": [f"$m.{k}", 0]}} for k in accumulators},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: MonthParam, department: Optional[str] = None):
    month_bounds(month)
    items = [
        {
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": float(Decimal(r["present_x2"]) / 2),
            "avg_work_hours": avg_hours(r),
            "late_count": r["late_count"],
            "total_late_minutes": r["late_total"],
            "leave_count": r["leave_count"],
            "on_duty_count": r["on_duty_count"],
        }
        for r in db.employees.aggregate(summary_pipeline(month, department))
    ]
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": date_range_match(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank is standard competition ranking: ties share a rank and the next one is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthParam, limit: LimitParam = 10, department: Optional[str] = None):
    month_bounds(month)
    items = list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))
    return {"month": month, "items": items}


def trend_range(date_from: str, date_to: str):
    start, end = parse_date(date_from, "from"), parse_date(date_to, "to")
    if end < start:
        raise bad_request("query", "to", "'to' must not be before 'from'")
    days = (end - start).days + 1
    if days > MAX_TREND_DAYS:
        raise bad_request("query", "to", f"range must not exceed {MAX_TREND_DAYS} days")
    return start, end, days


def trend_pipeline(department: str, date_from: str, date_to: str) -> list:
    start, end, days = trend_range(date_from, date_to)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=UTC)
    # 2 x present for one day's logs (a half day is 1), see month_accumulators
    is_present = {"$in": ["$status", PRESENCE_STATUSES]}
    half = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
    late = {"$ifNull": ["$late_minutes", 0]}
    day = {"$arrayElemAt": [{"$filter": {"input": "$days", "as": "d", "cond": {"$eq": ["$$d._id", "$row.date"]}}}, 0]}
    return [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joins": {"$push": "$joined_on"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "codes",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": date_range_match(start, end)}},
                    {
                        "$group": {
                            "_id": "$date",
                            "present_x2": {"$sum": {"$cond": [is_present, {"$cond": [half, 1, 2]}, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                        }
                    },
                ],
                "as": "days",
            }
        },
        # gap filling: one row for every calendar day in the range, whether or not there are logs
        {
            "$project": {
                "joins": 1,
                "days": 1,
                "row": {
                    "$map": {
                        "input": {"$range": [0, days]},
                        "as": "i",
                        "in": {
                            "date": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$i"}},
                                }
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$row"},
        {
            "$set": {
                "date": "$row.date",
                "is_working_day": {
                    "$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$row.date"}}}, [1, 2, 3, 4, 5]]
                },
                "headcount": {"$size": {"$filter": {"input": "$joins", "as": "j", "cond": {"$lte": ["$$j", "$row.date"]}}}},
                "present_x2": {"$ifNull": [day_field(day, "present_x2"), 0]},
                "late_count": {"$ifNull": [day_field(day, "late_count"), 0]},
            }
        },
        {
            # attendance_rate in integer units of 1e-4, half-up: floor(p / (2h) * 10^4 + 1/2) with integer arithmetic
            "$set": {
                "rate4": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$floor": {
                                "$divide": [
                                    {"$add": [{"$multiply": ["$present_x2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]},
                                ]
                            }
                        },
                        None,
                    ]
                }
            }
        },
        {"$set": {"has_rate": {"$cond": [{"$eq": ["$rate4", None]}, 0, 1]}}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "win_sum": {"$sum": "$rate4", "window": {"documents": [-6, "current"]}},
                    "win_n": {"$sum": "$has_rate", "window": {"documents": [-6, "current"]}},
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": {"$divide": ["$present_x2", 2]},
                "late_count": 1,
                "attendance_rate": {"$cond": [{"$eq": ["$rate4", None]}, None, {"$divide": ["$rate4", 10000]}]},
                # mean of the (rounded) rates in the window, rounded half-up: floor((2S + n) / 2n) / 10^4
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$win_n", 0]},
                        None,
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$divide": [
                                            {"$add": [{"$multiply": [2, "$win_sum"]}, "$win_n"]},
                                            {"$multiply": [2, "$win_n"]},
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                    ]
                },
            }
        },
    ]


def day_field(day_expr: dict, name: str) -> dict:
    return {"$getField": {"field": name, "input": day_expr}}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: Annotated[str, Query(alias="from")], date_to: Annotated[str, Query(alias="to")]):
    pipeline = trend_pipeline(department, date_from, date_to)  # validates the range first (422)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    rows = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": rows}


# --------------------------------------------------------------------------- #
# Explain
# --------------------------------------------------------------------------- #
def require(**params):
    for name, value in params.items():
        if value is None or value == "":
            raise bad_request("query", name, "this parameter is required for the endpoint being explained")


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]] = None,
    department: Optional[str] = None,
    limit: LimitParam = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[StatusName] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageParam = 1,
    page_size: PageSizeParam = 20,
):
    """Runs the same find/pipeline as the real endpoint under explain('executionStats') and returns it raw."""
    if endpoint == "attendance_list":
        q, sort, skip, lim = attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": dict(sort), "skip": skip, "limit": lim}
    else:
        if endpoint == "employee_monthly":
            require(emp_code=emp_code, month=month)
            collection, pipeline = "attendance_logs", monthly_pipeline(emp_code, month)
        elif endpoint == "department_summary":
            require(month=month)
            collection, pipeline = "employees", summary_pipeline(month, department)
        elif endpoint == "late_leaderboard":
            require(month=month)
            collection, pipeline = "attendance_logs", leaderboard_pipeline(month, limit, department)
        else:
            require(department=department, **{"from": from_, "to": to})
            collection, pipeline = "employees", trend_pipeline(department, from_, to)
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    try:
        raw = db.command({"explain": command, "verbosity": "executionStats"})
    except PyMongoError as exc:
        raise HTTPException(503, f"explain failed: {exc}")
    # BSON -> plain JSON (relaxed extended JSON keeps numbers as numbers)
    explain = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": explain}
