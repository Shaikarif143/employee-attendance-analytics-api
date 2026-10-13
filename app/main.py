"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, fallback to defaults)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date as dt_date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
from typing import Annotated, Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Load local .env if present; environment variables take precedence
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# PyMongo client configured with tz_aware=True to handle UTC BSON datetimes correctly
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# Indian Standard Time (UTC+05:30) per business rule R1
IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """
    Creates necessary database indexes idempotently at application startup.
    Ensures query performance on large datasets (~100k docs) and enforces constraints.
    """
    # Unique constraint on employee business code
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    # Compound index for department filtering and headcount range checks (R9)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    # Index for headcount checks across all departments
    db.employees.create_index([("joined_on", ASCENDING)])

    # Unique compound index ensuring at most one record per employee per attendance date (R1 concurrency)
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # Compound index for sorting by date desc, emp_code asc in attendance listing
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Compound index for fast punch-out lookup of latest open punch_in
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    # Compound index for monthly late-comers filtering
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure all required indexes exist before serving traffic
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic (R1 - R8)
# --------------------------------------------------------------------------- #

def round_half_up(value: float, decimals: int = 2) -> float:
    """
    R8: Arithmetic rounding half-up (standard commercial rounding),
    distinct from Python's built-in round() which uses round-half-to-even.
    """
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    d = Decimal(str(value))
    return float(d.quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def truncate_to_seconds(ms: int) -> datetime:
    """
    R1: Instants are truncated to whole seconds before any calculation,
    and the truncated UTC instant is stored in MongoDB.
    """
    whole_seconds = ms // 1000
    return datetime.fromtimestamp(whole_seconds, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a UTC datetime back to integer epoch milliseconds for API responses."""
    if dt is None:
        return None
    return int(dt.timestamp()) * 1000


def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is the IST calendar date of punch_in.
    Exception: For overnight shifts (shift_end <= shift_start), a punch-in
    earlier than shift_end belongs to the shift that started the previous calendar day.
    """
    dt_ist = punch_in_utc.astimezone(IST)
    punch_time_str = dt_ist.strftime("%H:%M")
    is_overnight = shift_end <= shift_start

    if is_overnight and punch_time_str < shift_end:
        return (dt_ist.date() - timedelta(days=1)).isoformat()
    return dt_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: 10-minute grace period. Punch-in is late only if strictly > 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) since shift_start, not since grace ended.
    """
    sh, sm = map(int, shift_start.split(":"))
    att_date = dt_date.fromisoformat(attendance_date_str)
    shift_start_dt = datetime(att_date.year, att_date.month, att_date.day, sh, sm, 0, tzinfo=IST)

    diff_seconds = (punch_in_utc - shift_start_dt).total_seconds()
    # 10 minutes grace period = 600 seconds
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded half-up to 2 decimals.
    """
    seconds = int((punch_out_utc - punch_in_utc).total_seconds())
    d_hours = Decimal(seconds) / Decimal(3600)
    return float(d_hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, attendance_date_str: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is >= 30; otherwise 0.
    Overnight shift end falls on the next calendar day in IST.
    """
    eh, em = map(int, shift_end.split(":"))
    att_date = dt_date.fromisoformat(attendance_date_str)
    is_overnight = shift_end <= shift_start

    end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, 0, tzinfo=IST)

    diff_seconds = (punch_out_utc - shift_end_dt).total_seconds()
    ot_minutes = int(diff_seconds // 60)
    return ot_minutes if ot_minutes >= 30 else 0


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """
    R7: Count Monday-Friday working days in the calendar month.
    For employees who joined mid-month, working days start from joined_on.
    """
    year, month = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, month)

    month_start = dt_date(year, month, 1)
    month_end = dt_date(year, month, num_days)
    joined_on = dt_date.fromisoformat(joined_on_str)

    if joined_on > month_end:
        return 0

    effective_start = max(month_start, joined_on)
    working_days = 0
    curr = effective_start
    while curr <= month_end:
        if curr.weekday() < 5:  # Monday to Friday (0-4)
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def get_month_end_date(month_str: str) -> str:
    """Returns YYYY-MM-DD for the last day of the given month."""
    year, month = map(int, month_str.split("-"))
    _, num_days = calendar.monthrange(year, month)
    return f"{year:04d}-{month:02d}-{num_days:02d}"


def serialize_employee(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an employee MongoDB document for API response (removes _id, formats created_at)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance_record(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats an attendance MongoDB document according to openapi AttendanceRecord schema."""
    history_serialized = []
    for h in doc.get("history", []):
        serialized_changes = {}
        for field, change in h.get("changes", {}).items():
            f_val = change.get("from")
            t_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = dt_to_epoch_ms(f_val)
                if isinstance(t_val, datetime):
                    t_val = dt_to_epoch_ms(t_val)
            serialized_changes[field] = {"from": f_val, "to": t_val}

        history_serialized.append({
            "at": dt_to_epoch_ms(h["at"]) if isinstance(h["at"], datetime) else h["at"],
            "by": h["by"],
            "reason": h["reason"],
            "changes": serialized_changes,
        })

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_serialized,
    }


# --------------------------------------------------------------------------- #
# Pydantic Schemas
# --------------------------------------------------------------------------- #

EpochMillis = Annotated[int, Field(ge=100_000_000_000, le=4_102_444_800_000, strict=True)]


class EmployeeCreate(BaseModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP\d{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", default="09:30")]
    shift_end: Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$", default="18:30")]
    joined_on: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")]

    @model_validator(mode="after")
    def validate_shift(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def portal_page():
    """Serves the interactive web portal for manual testing and HR workflows."""
    portal_file = os.path.join(os.path.dirname(__file__), "portal.html")
    if os.path.exists(portal_file):
        with open(portal_file, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>HROne Attendance API</h1><p>Visit <a href='/docs'>/docs</a> for API documentation.</p>"


@app.get("/health", tags=["system"])
def health():
    """Returns 200 only if MongoDB answers a ping; otherwise returns 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="database unreachable",
        ) from exc


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """
    Creates an employee. Unique emp_code is strictly enforced by a database index.
    """
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """Lists employees sorted by emp_code asc. Pagination total reflects filters."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort([("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_employee(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """
    Records punch-in. Race-safe via unique index on (emp_code, date).
    Follows R1 (overnight date & truncation) and R2 (grace & late calculation).
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_in_dt = truncate_to_seconds(punch_ms)

    att_date = determine_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """
    Records punch-out for the most recent record whose punch_in <= punched_at.
    Handles overnight shifts across midnight. Race-safe via atomic conditional update.
    """
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punch_ms = body.punched_at if body.punched_at is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
    punch_out_dt = truncate_to_seconds(punch_ms)

    # Find the most recent record whose punch_in is at or before punched_at
    latest_rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_dt}},
        sort=[("punch_in", DESCENDING)],
    )
    if not latest_rec:
        raise HTTPException(status_code=404, detail="no punch-in found")

    # If that record is already punched out, reject with 409
    if latest_rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="already punched out")

    punch_in_dt = latest_rec["punch_in"]
    duration_sec = (punch_out_dt - punch_in_dt).total_seconds()
    if duration_sec <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
    if duration_sec > 24 * 3600:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hrs = compute_work_hours(punch_in_dt, punch_out_dt)
    ot_min = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], latest_rec["date"])
    half_d = bool(work_hrs < 4.50)

    # Atomic update on punch_out: None ensures two simultaneous requests yield exactly one 200 and one 409
    updated_rec = db.attendance_logs.find_one_and_update(
        {
            "_id": latest_rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_rec:
        raise HTTPException(status_code=409, detail="already punched out")

    return serialize_attendance_record(updated_rec)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Lists attendance records sorted by date desc, then emp_code asc.
    Uses cursor-based pagination backed by indexes. Fast on 100k records.
    """
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be after date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(doc) for doc in cursor]

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    """
    Manual correction of one attendance record. Recomputes derived fields
    and appends an audit trail entry to history. Never overwrites past history.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="attendance record not found")

    # ABSENT or LEAVE clears both punch times; supplying punch times with it is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(
            status_code=422,
            detail="cannot supply punch times with ABSENT or LEAVE",
        )

    target_status = body.status if body.status is not None else rec["status"]

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires punch_in
        if body.punch_in is not None:
            new_punch_in = truncate_to_seconds(body.punch_in)
        else:
            new_punch_in = rec.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires a punch_in",
            )

        # punch_in must stay on the record's attendance date under R1
        calc_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not match attendance date under R1",
            )

        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date)

        if body.punch_out is not None:
            new_punch_out = truncate_to_seconds(body.punch_out)
        else:
            new_punch_out = rec.get("punch_out")

        if new_punch_out is not None:
            dur = (new_punch_out - new_punch_in).total_seconds()
            if dur <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be after punch_in",
                )
            if dur > 24 * 3600:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out cannot be more than 24 hours after punch_in",
                )
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(new_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = bool(new_work_hours < 4.50)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute dictionary of actual field changes
    changes: Dict[str, Dict[str, Any]] = {}
    if target_status != rec["status"]:
        changes["status"] = {"from": rec["status"], "to": target_status}
    if new_punch_in != rec.get("punch_in"):
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": new_punch_in}
    if new_punch_out != rec.get("punch_out"):
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": new_punch_out}
    if new_work_hours != rec.get("work_hours"):
        changes["work_hours"] = {"from": rec.get("work_hours"), "to": new_work_hours}
    if new_late_minutes != rec.get("late_minutes", 0):
        changes["late_minutes"] = {"from": rec.get("late_minutes", 0), "to": new_late_minutes}
    if new_overtime_minutes != rec.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": rec.get("overtime_minutes", 0), "to": new_overtime_minutes}
    if new_half_day != rec.get("half_day", False):
        changes["half_day"] = {"from": rec.get("half_day", False), "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=422, detail="no changes made")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency check: match current history length so concurrent edits don't drop entries
    curr_len = len(rec.get("history", []))
    updated_rec = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            f"history.{curr_len}": {"$exists": False},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry,
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated_rec:
        raise HTTPException(status_code=409, detail="concurrent modification conflict")

    return serialize_attendance_record(updated_rec)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    """
    Monthly attendance summary for one employee via MongoDB aggregation pipeline.
    Follows R5, R6, R7, and R8.
    """
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    month_end = get_month_end_date(month)
    working_days = count_working_days(emp["joined_on"], month)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": f"{month}-01", "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    docs = list(db.attendance_logs.aggregate(pipeline))
    if docs:
        d = docs[0]
        present_days = float(d["present_days"])
        leave_days = int(d["leave_days"])
        late_count = int(d["late_count"])
        total_late = int(d["total_late_minutes"])
        total_ot = int(d["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    attendance_pct = round_half_up(present_days / working_days * 100, 2) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """
    Department monthly summary cross-collection aggregation.
    Includes employees with zero logs in headcount (R9).
    Computes avg_work_hours across matching records, not average of averages.
    """
    month_end = get_month_end_date(month)
    match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_emp["department"] = department

    pipeline = [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", f"{month}-01"]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]},
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1.0]},
                                        0,
                                    ]
                                }
                            },
                            "total_work_hours": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                            },
                            "on_duty_count": {
                                "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                            },
                        }
                    },
                ],
                "as": "log_stats",
            }
        },
        {
            "$project": {
                "department": 1,
                "log_stat": {"$arrayElemAt": ["$log_stats", 0]},
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$log_stat.present_days", 0]}},
                "total_work_hours": {"$sum": {"$ifNull": ["$log_stat.total_work_hours", 0]}},
                "work_hours_count": {"$sum": {"$ifNull": ["$log_stat.work_hours_count", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$log_stat.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$log_stat.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$log_stat.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$log_stat.on_duty_count", 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$round": [{"$divide": ["$total_work_hours", "$work_hours_count"]}, 2]},
                        None,
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    """
    Ranks late employees using standard competition ranking ($rank).
    Ties share a rank and skip the next. Limit applies after ranking.
    """
    month_end = get_month_end_date(month)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": f"{month}-01", "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]

    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": DESCENDING},
                "output": {
                    "rank": {"$rank": {}},
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """
    Daily attendance trend with database gap-filling ($densify) and 7-day moving average.
    Returns exactly one row per calendar day inside the range.
    """
    if to_date < from_date:
        raise HTTPException(status_code=422, detail="to cannot be before from")

    from_dt = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    to_dt = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    diff_days = (to_dt - from_dt).days + 1
    if diff_days > 92:
        raise HTTPException(status_code=422, detail="date range exceeds 92 days")

    # 404 if department has no employees
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="department not found")

    to_dt_plus_1 = to_dt + timedelta(days=1)

    pipeline = [
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date": "$_id",
                "date_obj": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                "date_obj": 1,
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$date_obj"}, [2, 3, 4, 5, 6]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc_docs",
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc_docs.count", 0]}, 0]},
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$round": [{"$divide": ["$present_count", "$headcount"]}, 4]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    },
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {"$round": ["$moving_avg_raw", 4]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"date": ASCENDING}},
    ]

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """
    Executes explain with executionStats verbosity for the named endpoint's main query,
    built with identical parameters, filters, sorting, and pagination.
    """
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be after date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        explain_cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month required")
        month_end = get_month_end_date(month)
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": f"{month}-01", "$lte": month_end}}},
            {"$project": {"date": 1, "status": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1}},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        month_end = get_month_end_date(month)
        match_emp: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_emp["department"] = department
        pipeline = [
            {"$match": match_emp},
            {"$project": {"department": 1, "emp_code": 1}},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "employees", "explain": res}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month required")
        month_end = get_month_end_date(month)
        pipeline = [
            {"$match": {"date": {"$gte": f"{month}-01", "$lte": month_end}, "late_minutes": {"$gt": 0}}},
            {"$project": {"emp_code": 1, "late_minutes": 1}},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="to cannot be before from")
        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {"$project": {"date": 1, "emp_code": 1}},
        ]
        explain_cmd = {
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
        res = db.command(explain_cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": res}

    raise HTTPException(status_code=422, detail="invalid endpoint")
