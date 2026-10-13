# Employee Attendance & Analytics API

FastAPI and MongoDB 6+ backend for managing employee attendance, punch-in/out records, manual regularizations with an audit trail, and high-performance analytical aggregation pipelines.

Includes an **Interactive Web Dashboard Portal** accessible directly at `http://127.0.0.1:8000/`.

---

## Prerequisites

* **Python 3.11+** installed (`python --version`)
* **MongoDB 6.0+** running locally (or via MongoDB Atlas / Docker container)
* **Git** installed

---

## Step-by-Step Execution Guide

### 1. Clone the Repository
```bash
git clone https://github.com/Shaikarif143/employee-attendance-analytics-api.git
cd employee-attendance-analytics-api
```

### 2. Set Up a Virtual Environment

**On Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**On Windows (Command Prompt):**
```cmd
python -m venv .venv
.venv\Scripts\activate.bat
```

**On Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Configure Environment Variables
Create a `.env` file in the repository root (or copy from `.env.example`):
```bash
cp .env.example .env
```

Contents of `.env`:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
*(Environment variables take precedence over `.env` if exported directly).*

### 5. Ensure MongoDB is Running
Make sure MongoDB 6.0+ is running on your configured host/port:
* **Local Service (Windows/Linux):** Start the MongoDB service or run `mongod --dbpath <data_dir>`.
* **Docker Container (Optional):**
  ```bash
  docker run -d --name mongo -p 27017:27017 mongo:7.0
  ```
* **MongoDB Atlas:** Set `MONGO_URI="mongodb+srv://<user>:<password>@cluster.mongodb.net/..."` in `.env`.

### 6. Seed Sample Data
Load the initial employee shifts and sample attendance logs:
```bash
python sample_seed.py
```
Expected output:
```text
loaded  3 documents into 'attendance_db.employees'
loaded  6 documents into 'attendance_db.attendance_logs'
```

### 7. Run the API Server
Start the Uvicorn ASGI server with live reloading enabled:
```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

When the server starts, it will automatically:
1. Connect to MongoDB and ping the server.
2. Initialize and verify all compound and unique indexes idempotently.
3. Serve the interactive Web Portal and REST endpoints.

---

## Accessing the Application

| Resource | URL | Description |
| :--- | :--- | :--- |
| **Interactive Web Portal** | [http://127.0.0.1:8000/](http://127.0.0.1:8000/) | Live UI: Virtual Punch Terminal, KPI Cards, Late Leaderboard, Department Trends, Attendance Log Viewer with Regularization Modal, and Index Scan Tester. |
| **Interactive Swagger Docs** | [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) | Interactive OpenAPI / Swagger UI to test every API endpoint directly in your browser. |
| **ReDoc API Reference** | [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc) | Clean, readable API reference documentation. |
| **Health Check Endpoint** | [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health) | Returns `{"status": "ok"}` when server and database are healthy. |

---

## Running Automated Tests

Run the full pytest suite covering health checks, employee CRUD, punch lifecycle, race condition safety, aggregation pipelines, and query explain execution plans:

```bash
pytest tests/test_api.py -v
```

Expected test suite results:
```text
tests/test_api.py::test_health PASSED
tests/test_api.py::test_employee_crud PASSED
tests/test_api.py::test_punch_in_punch_out_lifecycle PASSED
tests/test_api.py::test_regularize_record PASSED
tests/test_api.py::test_concurrency_race_safety PASSED
tests/test_api.py::test_analytics_endpoints PASSED
tests/test_api.py::test_admin_explain PASSED

======================== 7 passed in ~0.35s ========================
```

---

## Example API Operations (cURL)

### 1. Punch In
```bash
curl -X POST "http://127.0.0.1:8000/attendance/punch-in" \
  -H "Content-Type: application/json" \
  -d '{"emp_code": "EMP0001", "punched_at": 1788927300000, "status": "PRESENT"}'
```

### 2. Punch Out
```bash
curl -X POST "http://127.0.0.1:8000/attendance/punch-out" \
  -H "Content-Type: application/json" \
  -d '{"emp_code": "EMP0001", "punched_at": 1788961500000}'
```

### 3. Manual Regularization (Audit Trail)
```bash
curl -X PATCH "http://127.0.0.1:8000/attendance/EMP0001/2026-09-09" \
  -H "Content-Type: application/json" \
  -d '{
    "punch_in": 1788926400000,
    "reason": "Biometric terminal calibration delay",
    "regularized_by": "manager.qa"
  }'
```

### 4. Late Leaderboard (Competition Ranking)
```bash
curl -X GET "http://127.0.0.1:8000/analytics/leaderboard/late?month=2026-07&limit=5"
```

### 5. Verify Index Scans (No COLLSCAN)
```bash
curl -X GET "http://127.0.0.1:8000/admin/explain/late_leaderboard?month=2026-07"
```

---

## Architecture & Implementation Highlights

* **Pure MongoDB Aggregation Pipelines:** Analytics endpoints (`/analytics/employees/{emp_code}/monthly`, `/analytics/departments/summary`, `/analytics/leaderboard/late`, `/analytics/departments/{department}/trend`) execute 100% inside MongoDB aggregation pipelines without in-memory Python looping.
* **Competition Ranking with Ties:** The late leaderboard utilizes MongoDB's `$setWindowFields` with `$rank` to provide standard competition ranking (1, 2, 2, 4), and applies limits strictly after ranking.
* **Calendar Gap-Filling:** The department trend endpoint uses `$densify` with explicit bounds to generate continuous daily series, and calculates 7-day moving averages via `$setWindowFields`.
* **Concurrency & Race Safety:**
  * Punch-in uniqueness is strictly enforced via a compound unique index on `(emp_code, date)` (catching `DuplicateKeyError` to return HTTP 409).
  * Punch-out race conditions are resolved atomically via `find_one_and_update` matching `punch_out: None`.
  * Manual regularizations (`PATCH`) maintain an immutable audit trail (`history`) without overwriting previous history entries.
* **Automatic Indexes:** All required compound and unique indexes are created automatically and idempotently during application startup (`lifespan`).
* **Query Performance:** The `/admin/explain/{endpoint}` endpoint demonstrates index scans (`IXSCAN`) and absence of collection scans (`COLLSCAN`).

---

## Missing or Partial Features
* **None.** All 12 endpoints and business rules R1 through R10 from `openapi.yaml` and `PROBLEM_STATEMENT.docx` are fully implemented, tested, and verified.
