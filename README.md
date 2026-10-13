# Employee Attendance & Analytics API

FastAPI and MongoDB 6+ backend for managing employee attendance, punch-in/out records, manual regularizations with an audit trail, and analytical aggregation pipelines.

## Requirements
* Python 3.11+
* MongoDB 6.0+ (Local Docker or MongoDB Atlas)

## Quick Start

### 1. Installation
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Environment Configuration
Create a `.env` file (or export environment variables directly):
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```
Real environment variables take precedence over `.env`.

### 3. Load Sample Data (Optional)
```bash
python sample_seed.py
```

### 4. Run the API Server
```bash
uvicorn app.main:app --port 8000 --reload
```
The API is available at `http://127.0.0.1:8000`. Interactive OpenAPI documentation is accessible at `http://127.0.0.1:8000/docs`.

## Testing

Run tests to verify all endpoints, business rules, concurrency constraints, and aggregation explain plans:
```bash
python sample_seed.py
python -m pytest tests/  # or run test scripts directly
```

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

## Missing or Partial Features
* None. All 12 endpoints and business rules R1 through R10 from `openapi.yaml` and `PROBLEM_STATEMENT.docx` are fully implemented and verified.
