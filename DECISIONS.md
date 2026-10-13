# DECISIONS.md

<!-- [YOUR_REWRITE_MARKER: Feel free to rewrite each point in your own personal style] -->

### 1. Indexes
I created:
* `employees`: unique `{"emp_code": 1}` to enforce unique employee identity; compound `{"department": 1, "joined_on": 1}` to support department filtering and headcount aggregations.
* `attendance_logs`: unique `{"emp_code": 1, "date": 1}` to enforce one record per day and prevent duplicate punch-ins; compound `{"date": -1, "emp_code": 1}` for fast pagination in `GET /attendance`; compound `{"emp_code": 1, "punch_in": -1}` for instant punch-out lookups; and `{"date": 1, "late_minutes": -1}` for the late leaderboard.
* **Considered & rejected:** An index on `status` alone. It has low cardinality (only 5 values), so an index scan provides little selectivity compared to compound date/employee indexes.

### 2. Punch-in race
When two identical requests arrive simultaneously, both compute the attendance date and attempt insertion. The database unique index on `(emp_code, date)` acts as the source of truth. Exactly one insert succeeds and returns HTTP 201. The other raises a MongoDB `DuplicateKeyError`. Our code catches this exception and returns HTTP 409 Conflict. No race condition can bypass the index.

### 3. Ties
The leaderboard computes standard competition ranking via MongoDB's `$rank` window function (ranks: 1, 2, 2, 4). Per the contract, the filter `rank <= limit` applies *after* ranking. If two employees tie at the cutoff rank, both are returned. The result list can have more than `limit` rows, preventing arbitrary exclusions. Tied rows sort deterministically by `emp_code` ascending.

### 4. Headcount
The department summary aggregation starts on the `employees` collection, not `attendance_logs`. It filters all employees with `joined_on <= month_end`. It then joins attendance records using `$lookup`. Employees with zero logs produce an empty array, but still count as `1` towards department headcount.

### 5. Serving 100x the data
At 100x scale (~10 million logs), running real-time aggregations on every request would degrade latency. I would pre-compute daily/monthly metrics into a materialized roll-up collection using background workers or MongoDB time-series bucket patterns.
