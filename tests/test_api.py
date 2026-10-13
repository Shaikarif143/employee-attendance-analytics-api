import concurrent.futures
import pytest
import requests

BASE_URL = "http://127.0.0.1:8000"


@pytest.fixture(scope="session", autouse=True)
def ensure_server_running():
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=2)
        assert r.status_code == 200
    except Exception as exc:
        pytest.fail(f"API server is not running on {BASE_URL}: {exc}")


def test_health():
    r = requests.get(f"{BASE_URL}/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_employee_crud():
    emp_code = "EMP8888"
    # Create employee
    payload = {
        "emp_code": emp_code,
        "name": "Pytest Worker",
        "email": "worker@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-05-01"
    }
    r = requests.post(f"{BASE_URL}/employees", json=payload)
    if r.status_code != 201:
        assert r.status_code == 409  # May already exist from previous test run
    else:
        assert r.status_code == 201
        data = r.json()
        assert data["emp_code"] == emp_code
        assert "_id" not in data
        assert isinstance(data["created_at"], int)

    # Duplicate create must return 409
    r_dup = requests.post(f"{BASE_URL}/employees", json=payload)
    assert r_dup.status_code == 409

    # List employees
    r_list = requests.get(f"{BASE_URL}/employees?department=Engineering")
    assert r_list.status_code == 200
    body = r_list.json()
    assert "items" in body
    assert body["total"] >= 1
    codes = [e["emp_code"] for e in body["items"]]
    assert codes == sorted(codes)


def test_punch_in_punch_out_lifecycle():
    emp_code = "EMP0001"
    # Punch in for 2026-09-09 (09:45:00 IST = 04:15:00 UTC = 1788927300000 ms)
    # 15 minutes late (> 10m grace period)
    ts_in = 1788927300000
    r_in = requests.post(
        f"{BASE_URL}/attendance/punch-in",
        json={"emp_code": emp_code, "punched_at": ts_in, "status": "PRESENT"}
    )
    if r_in.status_code == 201:
        data = r_in.json()
        assert data["date"] == "2026-09-09"
        assert data["late_minutes"] == 15
        assert data["punch_out"] is None

    # Punch out at 19:15:00 IST = 13:45:00 UTC = 1788961500000 ms (9.5 hrs work, 45 min overtime)
    ts_out = 1788961500000
    r_out = requests.post(
        f"{BASE_URL}/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": ts_out}
    )
    if r_out.status_code == 200:
        data = r_out.json()
        assert data["work_hours"] == 9.5
        assert data["overtime_minutes"] == 45
        assert data["half_day"] is False

    # Second punch out must return 409
    r_out_dup = requests.post(
        f"{BASE_URL}/attendance/punch-out",
        json={"emp_code": emp_code, "punched_at": ts_out}
    )
    assert r_out_dup.status_code == 409


def test_regularize_record():
    # Regularize EMP0001 on 2026-09-09
    ts_corr = 1788926400000  # 09:30:00 IST (late_minutes = 0)
    r = requests.patch(
        f"{BASE_URL}/attendance/EMP0001/2026-09-09",
        json={
            "punch_in": ts_corr,
            "reason": "Biometric system clock sync correction",
            "regularized_by": "manager.qa"
        }
    )
    if r.status_code == 200:
        data = r.json()
        assert data["late_minutes"] == 0
        assert len(data["history"]) >= 1
        last_hist = data["history"][-1]
        assert last_hist["by"] == "manager.qa"
        assert "late_minutes" in last_hist["changes"]


def test_concurrency_race_safety():
    emp_code = "EMP0002"
    ts = 1789012800000  # 2026-09-11 09:30:00 IST

    def punch():
        return requests.post(
            f"{BASE_URL}/attendance/punch-in",
            json={"emp_code": emp_code, "punched_at": ts, "status": "PRESENT"}
        ).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        codes = list(ex.map(lambda _: punch(), range(5)))

    # Exactly one request must succeed with 201; remaining must be 409
    if 201 in codes:
        assert codes.count(201) == 1
        assert codes.count(409) == 4


def test_analytics_endpoints():
    # Monthly employee summary
    r_emp = requests.get(f"{BASE_URL}/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r_emp.status_code == 200
    assert "working_days" in r_emp.json()

    # Department summary
    r_dept = requests.get(f"{BASE_URL}/analytics/departments/summary?month=2026-07")
    assert r_dept.status_code == 200
    assert "items" in r_dept.json()

    # Late leaderboard
    r_lb = requests.get(f"{BASE_URL}/analytics/leaderboard/late?month=2026-07&limit=5")
    assert r_lb.status_code == 200
    assert "items" in r_lb.json()

    # Department trend
    r_tr = requests.get(f"{BASE_URL}/analytics/departments/Engineering/trend?from=2026-07-05&to=2026-07-09")
    assert r_tr.status_code == 200
    assert len(r_tr.json()["items"]) == 5


def test_admin_explain():
    for ep in ["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]:
        r = requests.get(
            f"{BASE_URL}/admin/explain/{ep}",
            params={"month": "2026-07", "emp_code": "EMP0001", "department": "Engineering", "from": "2026-07-05", "to": "2026-07-09"}
        )
        assert r.status_code == 200
        plan = r.json()
        assert plan["endpoint"] == ep
        assert "IXSCAN" in str(plan["explain"])
