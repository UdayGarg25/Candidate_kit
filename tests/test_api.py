from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app import main

IST = main.IST


def ms(y, mo, d, h, mi, s=0, micro=0):
    """Epoch ms of an IST wall-clock time."""
    return int(datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp() * 1000) + micro // 1000


def mk_emp(client, code, dept="Eng", start="09:30", end="18:30", joined="2026-01-01"):
    r = client.post("/employees", json=dict(emp_code=code, name="N " + code, email=f"{code}@x.com", department=dept,
                                            shift_start=start, shift_end=end, joined_on=joined))
    assert r.status_code == 201, r.text
    return r.json()


def put_log(db, code, date, status="PRESENT", late=0, ot=0, wh=None, half=False, pin=None, pout=None):
    db.attendance_logs.insert_one(dict(emp_code=code, date=date, status=status, punch_in=pin, punch_out=pout,
                                       work_hours=wh, late_minutes=late, overtime_minutes=ot, half_day=half, history=[]))


# ------------------------------- pure rules ---------------------------------
def dt(h, m, s=0, day=6):
    return datetime(2026, 7, day, h, m, s, tzinfo=IST).astimezone(timezone.utc)


def test_late_boundaries_r2():
    d = datetime(2026, 7, 6).date()
    f = lambda t: main.compute_late_minutes(t, d, "09:30")
    assert f(dt(9, 40, 0)) == 0
    assert f(dt(9, 40, 1)) == 10
    assert f(dt(10, 15, 59)) == 45
    assert f(dt(9, 0)) == 0


def test_overtime_boundaries_r3():
    d = datetime(2026, 7, 6).date()
    f = lambda t: main.compute_overtime(t, d, "09:30", "18:30")
    assert f(dt(18, 59, 59)) == 0       # 29 min 59 s -> floor 29 -> not counted
    assert f(dt(19, 0, 0)) == 30
    assert f(dt(19, 0, 59)) == 30
    assert f(dt(18, 0)) == 0


def test_overtime_overnight():
    d = datetime(2026, 7, 6).date()
    out = datetime(2026, 7, 7, 6, 40, tzinfo=IST)
    assert main.compute_overtime(out, d, "22:00", "06:00") == 40


def test_work_hours_half_up_r4():
    t0 = dt(9, 0)
    assert main.compute_work_hours(t0, t0 + timedelta(hours=9, seconds=18)) == 9.01   # 9.005 -> half-up (round() gives 9.0)
    assert main.compute_work_hours(t0, t0 + timedelta(hours=1, seconds=9)) == 1.0     # 1.0025
    assert main.compute_work_hours(t0, t0 + timedelta(hours=1, seconds=27)) == 1.01   # 1.0075


def test_half_day_uses_rounded_hours_r5():
    t0 = dt(9, 0)
    wh = main.compute_work_hours(t0, t0 + timedelta(seconds=16182))  # 4.495 -> 4.50
    assert wh == 4.5 and not main.is_half_day(wh)
    wh = main.compute_work_hours(t0, t0 + timedelta(seconds=16164))  # 4.49
    assert wh == 4.49 and main.is_half_day(wh)


def test_attendance_date_overnight_r1():
    f = lambda t: main.attendance_date(t, "22:00", "06:00").isoformat()
    assert f(datetime(2026, 7, 6, 21, 55, tzinfo=IST)) == "2026-07-06"
    assert f(datetime(2026, 7, 7, 1, 0, tzinfo=IST)) == "2026-07-06"      # after midnight -> previous day
    assert f(datetime(2026, 7, 7, 5, 59, 59, tzinfo=IST)) == "2026-07-06"
    assert f(datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)) == "2026-07-07"   # not earlier than shift_end
    assert main.attendance_date(datetime(2026, 7, 6, 0, 10, tzinfo=IST), "09:30", "18:30").isoformat() == "2026-07-06"


# ------------------------------- employees ----------------------------------
def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_employee_create_validation_and_duplicates(client):
    body = dict(emp_code="EMP9001", name="A", email="a@b.co", department="V", joined_on="2026-01-01")
    r = client.post("/employees", json=body)
    assert r.status_code == 201
    e = r.json()
    assert e["shift_start"] == "09:30" and e["shift_end"] == "18:30" and isinstance(e["created_at"], int)
    assert "_id" not in e
    assert client.post("/employees", json=body).status_code == 409
    for patch in [dict(emp_code="E1"), dict(emp_code="EMP12"), dict(email="nope"), dict(joined_on="2026-13-01"),
                  dict(joined_on="2026-1-1"), dict(shift_start="25:00"), dict(shift_start="09:30", shift_end="09:30"),
                  dict(name=""), dict(department="x" * 51)]:
        assert client.post("/employees", json={**body, "emp_code": "EMP9002", **patch}).status_code == 422, patch
    assert client.post("/employees", json={k: v for k, v in body.items() if k != "name"}).status_code == 422


def test_employee_create_concurrent_duplicates(client):
    body = dict(emp_code="EMP9100", name="A", email="a@b.co", department="V", joined_on="2026-01-01")
    with ThreadPoolExecutor(8) as ex:
        codes = sorted(r.status_code for r in ex.map(lambda _: client.post("/employees", json=body), range(8)))
    assert codes == [201] + [409] * 7


def test_employee_list_pagination_and_filter(client):
    for i in range(5):
        mk_emp(client, f"EMP80{i:02d}", dept="PagDept")
    r = client.get("/employees", params=dict(department="PagDept", page_size=2, page=2)).json()
    assert [e["emp_code"] for e in r["items"]] == ["EMP8002", "EMP8003"]
    assert r["total"] == 5 and r["page"] == 2 and r["page_size"] == 2
    assert client.get("/employees", params=dict(department="pagdept")).json()["total"] == 0  # case-sensitive
    assert client.get("/employees?page=0").status_code == 422
    assert client.get("/employees?page_size=101").status_code == 422
    assert client.get("/employees?page_size=0").status_code == 422


# ------------------------------- punch-in / out -----------------------------
def test_punch_in_basic_and_errors(client):
    mk_emp(client, "EMP7001")
    t = ms(2026, 7, 6, 10, 15, 59, 900_000)
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7001", punched_at=t))
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["date"] == "2026-07-06" and rec["late_minutes"] == 45 and rec["status"] == "PRESENT"
    assert rec["punch_in"] == t - 900 and rec["punch_in"] % 1000 == 0      # truncated to the second
    assert rec["punch_out"] is None and rec["work_hours"] is None and rec["history"] == [] and rec["half_day"] is False
    assert client.post("/attendance/punch-in", json=dict(emp_code="EMP7001", punched_at=t + 5000)).status_code == 409
    assert client.post("/attendance/punch-in", json=dict(emp_code="NOPE", punched_at=t)).status_code == 404
    for bad in [t // 1000, float(t), str(t), 2 * 10**13, True]:
        assert client.post("/attendance/punch-in", json=dict(emp_code="EMP7001", punched_at=bad)).status_code == 422, bad
    for st in ("ABSENT", "LEAVE", "bogus"):
        assert client.post("/attendance/punch-in", json=dict(emp_code="EMP7001", punched_at=t, status=st)).status_code == 422


def test_punch_in_grace_boundary(client):
    mk_emp(client, "EMP7002")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7002", punched_at=ms(2026, 7, 6, 9, 40, 0, 999_000)))
    assert r.json()["late_minutes"] == 0   # 09:40:00.999 truncates to 09:40:00 -> not late
    mk_emp(client, "EMP7003")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7003", punched_at=ms(2026, 7, 6, 9, 40, 1)))
    assert r.json()["late_minutes"] == 10


def test_punch_in_wfh_status_and_ignored_derived_fields(client):
    mk_emp(client, "EMP7004")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7004", punched_at=ms(2026, 7, 6, 9, 0), status="WFH",
                                                      late_minutes=99, work_hours=5, half_day=True))
    assert r.status_code == 201 and r.json()["status"] == "WFH" and r.json()["late_minutes"] == 0
    assert r.json()["half_day"] is False


def test_punch_in_race_exactly_one_201(client, db):
    mk_emp(client, "EMP7010")
    body = dict(emp_code="EMP7010", punched_at=ms(2026, 7, 6, 9, 0))
    with ThreadPoolExecutor(16) as ex:
        codes = sorted(r.status_code for r in ex.map(lambda _: client.post("/attendance/punch-in", json=body), range(16)))
    assert codes == [201] + [409] * 15
    assert db.attendance_logs.count_documents({"emp_code": "EMP7010"}) == 1


def test_punch_out_flow_and_rules(client):
    mk_emp(client, "EMP7020")
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7020", punched_at=ms(2026, 7, 6, 10, 5)))
    out = ms(2026, 7, 6, 19, 10)
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7020", punched_at=out))
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["work_hours"] == 9.08 and rec["overtime_minutes"] == 40 and rec["half_day"] is False
    assert rec["late_minutes"] == 35 and rec["punch_out"] == out and rec["history"] == []
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7020", punched_at=out + 60000)).status_code == 409
    assert client.post("/attendance/punch-out", json=dict(emp_code="NOPE", punched_at=out)).status_code == 404


def test_punch_out_errors(client):
    mk_emp(client, "EMP7021")
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=ms(2026, 7, 6, 18, 0))).status_code == 404
    pin = ms(2026, 7, 6, 9, 0)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7021", punched_at=pin))
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=pin)).status_code == 422       # equal
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=pin + 999)).status_code == 422  # same second
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=pin + 24 * 3600_000 + 1000)).status_code == 422
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=pin - 1000)).status_code == 404
    assert client.post("/attendance/punch-out", json=dict(emp_code="EMP7021", punched_at=pin + 24 * 3600_000)).status_code == 200


def test_punch_out_half_day_and_overtime_boundary(client):
    mk_emp(client, "EMP7022")
    pin = ms(2026, 7, 6, 9, 0)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7022", punched_at=pin, status="WFH"))
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7022", punched_at=pin + 16164_000)).json()
    assert r["work_hours"] == 4.49 and r["half_day"] is True
    mk_emp(client, "EMP7023")
    pin = ms(2026, 7, 6, 9, 0)
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7023", punched_at=pin))
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7023", punched_at=ms(2026, 7, 6, 18, 59, 59))).json()
    assert r["overtime_minutes"] == 0
    assert r["work_hours"] == 10.0   # 35999 s = 9.9997
    mk_emp(client, "EMP7025")
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7025", punched_at=pin))
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7025", punched_at=pin + 16182_000)).json()
    assert r["work_hours"] == 4.5 and r["half_day"] is False   # 4.495 rounds to 4.50 first, so not a half day


def test_punch_out_race_exactly_one_200(client):
    mk_emp(client, "EMP7024")
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7024", punched_at=ms(2026, 7, 6, 9, 0)))
    body = dict(emp_code="EMP7024", punched_at=ms(2026, 7, 6, 18, 0))
    with ThreadPoolExecutor(12) as ex:
        codes = sorted(r.status_code for r in ex.map(lambda _: client.post("/attendance/punch-out", json=body), range(12)))
    assert codes == [200] + [409] * 11


def test_overnight_shift_punch_in_and_out(client):
    mk_emp(client, "EMP7030", dept="Night", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7030", punched_at=ms(2026, 7, 6, 21, 55)))
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 0
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7030", punched_at=ms(2026, 7, 7, 6, 40)))
    assert r.status_code == 200
    assert r.json()["work_hours"] == 8.75 and r.json()["overtime_minutes"] == 40
    # a punch-in after midnight belongs to the previous day's shift and is late relative to that day's start
    mk_emp(client, "EMP7031", dept="Night", start="22:00", end="06:00")
    r = client.post("/attendance/punch-in", json=dict(emp_code="EMP7031", punched_at=ms(2026, 7, 7, 0, 30)))
    assert r.json()["date"] == "2026-07-06" and r.json()["late_minutes"] == 150


def test_punch_in_for_two_days_then_punch_out_picks_latest(client):
    mk_emp(client, "EMP7032")
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7032", punched_at=ms(2026, 7, 6, 9, 0)))
    client.post("/attendance/punch-out", json=dict(emp_code="EMP7032", punched_at=ms(2026, 7, 6, 18, 0)))
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7032", punched_at=ms(2026, 7, 7, 9, 0)))
    r = client.post("/attendance/punch-out", json=dict(emp_code="EMP7032", punched_at=ms(2026, 7, 7, 17, 0)))
    assert r.status_code == 200 and r.json()["date"] == "2026-07-07"


# ------------------------------- listing ------------------------------------
def test_attendance_list_filters_sort_pagination(client):
    mk_emp(client, "EMP7040", dept="L")
    mk_emp(client, "EMP7041", dept="L")
    for d in (6, 7, 8):
        for c in ("EMP7040", "EMP7041"):
            client.post("/attendance/punch-in", json=dict(emp_code=c, punched_at=ms(2026, 9, d, 9, 0)))
    r = client.get("/attendance", params=dict(date_from="2026-09-06", date_to="2026-09-08", page_size=4)).json()
    assert r["total"] == 6 and len(r["items"]) == 4
    assert [(i["date"], i["emp_code"]) for i in r["items"]] == [("2026-09-08", "EMP7040"), ("2026-09-08", "EMP7041"),
                                                                 ("2026-09-07", "EMP7040"), ("2026-09-07", "EMP7041")]
    r = client.get("/attendance", params=dict(emp_code="EMP7041", date_from="2026-09-07", date_to="2026-09-07")).json()
    assert r["total"] == 1 and r["items"][0]["emp_code"] == "EMP7041" and "_id" not in r["items"][0]
    r = client.get("/attendance", params=dict(emp_code="EMP7041", status="WFH")).json()
    assert r["total"] == 0
    assert client.get("/attendance", params=dict(date_from="2026-09-08", date_to="2026-09-07")).status_code == 422
    assert client.get("/attendance", params=dict(date_from="bad")).status_code == 422
    assert client.get("/attendance", params=dict(status="NOPE")).status_code == 422
    assert client.get("/attendance", params=dict(page=0)).status_code == 422


def test_legacy_record_defaults(client, db):
    db.attendance_logs.insert_one(dict(emp_code="EMP7050", date="2025-01-02", status="PRESENT",
                                       punch_in=datetime(2025, 1, 2, 4, 0, tzinfo=timezone.utc),
                                       punch_out=datetime(2025, 1, 2, 13, 0, tzinfo=timezone.utc), work_hours=9.0))
    r = client.get("/attendance", params=dict(emp_code="EMP7050")).json()["items"][0]
    assert r["history"] == [] and r["half_day"] is False and r["late_minutes"] == 0 and r["overtime_minutes"] == 0


# ------------------------------- regularize ---------------------------------
def setup_closed_day(client, code, start="09:30", end="18:30", day=6, pin=(10, 5), pout=(19, 10)):
    mk_emp(client, code, start=start, end=end)
    client.post("/attendance/punch-in", json=dict(emp_code=code, punched_at=ms(2026, 7, day, *pin)))
    client.post("/attendance/punch-out", json=dict(emp_code=code, punched_at=ms(2026, 7, day, *pout)))


def test_regularize_updates_and_appends_history(client):
    setup_closed_day(client, "EMP7100")
    new_in = ms(2026, 7, 6, 9, 28)
    r = client.patch("/attendance/EMP7100/2026-07-06", json=dict(punch_in=new_in, reason="biometric glitch", regularized_by="hr"))
    assert r.status_code == 200, r.text
    rec = r.json()
    assert rec["late_minutes"] == 0 and rec["punch_in"] == new_in and rec["work_hours"] == 9.7
    assert len(rec["history"]) == 1
    h = rec["history"][0]
    assert h["by"] == "hr" and h["reason"] == "biometric glitch" and isinstance(h["at"], int)
    assert h["changes"] == {
        "punch_in": {"from": ms(2026, 7, 6, 10, 5), "to": new_in},
        "late_minutes": {"from": 35, "to": 0},
        "work_hours": {"from": 9.08, "to": 9.7},
    }
    # a second correction appends; the first entry is untouched
    r = client.patch("/attendance/EMP7100/2026-07-06", json=dict(punch_out=ms(2026, 7, 6, 18, 0), reason="fix out time", regularized_by="hr2"))
    hist = r.json()["history"]
    assert len(hist) == 2 and hist[0] == rec["history"][0]
    assert set(hist[1]["changes"]) == {"punch_out", "work_hours", "overtime_minutes"}
    assert hist[1]["changes"]["overtime_minutes"] == {"from": 40, "to": 0}


def test_regularize_status_changes(client):
    setup_closed_day(client, "EMP7101")
    r = client.patch("/attendance/EMP7101/2026-07-06", json=dict(status="LEAVE", reason="approved leave", regularized_by="hr"))
    rec = r.json()
    assert r.status_code == 200 and rec["punch_in"] is None and rec["punch_out"] is None and rec["work_hours"] is None
    assert set(rec["history"][0]["changes"]) == {"status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes"}
    # back to a presence status needs a punch_in
    r = client.patch("/attendance/EMP7101/2026-07-06", json=dict(status="PRESENT", reason="reverted leave", regularized_by="hr"))
    assert r.status_code == 422
    r = client.patch("/attendance/EMP7101/2026-07-06", json=dict(status="WFH", punch_in=ms(2026, 7, 6, 9, 0), reason="reverted leave", regularized_by="hr"))
    assert r.status_code == 200 and r.json()["status"] == "WFH" and r.json()["work_hours"] is None


def test_regularize_validation(client):
    setup_closed_day(client, "EMP7102")
    url = "/attendance/EMP7102/2026-07-06"
    base = dict(reason="valid reason", regularized_by="hr")
    ok_in = ms(2026, 7, 6, 9, 0)
    assert client.patch(url, json=dict(status="ABSENT", punch_in=ok_in, **base)).status_code == 422
    assert client.patch(url, json=dict(status="LEAVE", punch_out=ms(2026, 7, 6, 12, 0), **base)).status_code == 422
    assert client.patch(url, json=dict(punch_in=ms(2026, 7, 7, 9, 0), **base)).status_code == 422       # other day
    assert client.patch(url, json=dict(punch_out=ms(2026, 7, 6, 10, 0), **base)).status_code == 422     # before punch_in (10:05)
    assert client.patch(url, json=dict(punch_out=ms(2026, 7, 7, 10, 6), **base)).status_code == 422     # > 24 h
    assert client.patch(url, json=dict(punch_in=ok_in, reason="abc", regularized_by="hr")).status_code == 422
    assert client.patch(url, json=dict(punch_in=ok_in, reason="valid reason")).status_code == 422
    assert client.patch(url, json=dict(punch_in=ok_in // 1000, **base)).status_code == 422
    assert client.patch(url, json=dict(punch_in=None, **base)).status_code == 422
    assert client.patch(url, json=dict(**base)).status_code == 422                                       # changes nothing
    same = ms(2026, 7, 6, 10, 5)
    assert client.patch(url, json=dict(punch_in=same, status="PRESENT", **base)).status_code == 422     # changes nothing
    assert client.patch("/attendance/EMP7102/2026-07-09", json=dict(punch_in=ok_in, **base)).status_code == 404
    assert client.patch("/attendance/NOPE/2026-07-06", json=dict(punch_in=ok_in, **base)).status_code == 404
    assert client.patch("/attendance/EMP7102/2026-13-06", json=dict(punch_in=ok_in, **base)).status_code == 422
    assert client.get("/attendance", params=dict(emp_code="EMP7102")).json()["items"][0]["history"] == []  # nothing leaked through


def test_regularize_overnight_date_rule(client):
    mk_emp(client, "EMP7103", start="22:00", end="06:00")
    client.post("/attendance/punch-in", json=dict(emp_code="EMP7103", punched_at=ms(2026, 7, 6, 21, 55)))
    ok = client.patch("/attendance/EMP7103/2026-07-06", json=dict(punch_in=ms(2026, 7, 7, 1, 0), reason="moved later", regularized_by="hr"))
    assert ok.status_code == 200 and ok.json()["late_minutes"] == 180
    bad = client.patch("/attendance/EMP7103/2026-07-06", json=dict(punch_in=ms(2026, 7, 7, 7, 0), reason="moved later", regularized_by="hr"))
    assert bad.status_code == 422


def test_regularize_concurrent_no_lost_history(client):
    setup_closed_day(client, "EMP7104")
    times = [ms(2026, 7, 6, 9, m) for m in range(0, 12)]

    def call(t):
        return client.patch("/attendance/EMP7104/2026-07-06", json=dict(punch_in=t, reason="parallel edit", regularized_by="hr"))

    with ThreadPoolExecutor(12) as ex:
        results = list(ex.map(call, times))
    codes = [r.status_code for r in results]
    assert set(codes) <= {200, 409} and codes.count(200) >= 1
    final = client.get("/attendance", params=dict(emp_code="EMP7104")).json()["items"][0]
    assert len(final["history"]) == codes.count(200)
    # the history is a consistent chain: each entry's "from" is the previous entry's "to"
    chain = [h["changes"]["punch_in"] for h in final["history"] if "punch_in" in h["changes"]]
    for prev, nxt in zip(chain, chain[1:]):
        assert prev["to"] == nxt["from"]


# ------------------------------- analytics ----------------------------------
@pytest.fixture(scope="module")
def march(client, db):
    """Synthetic March 2026 (22 weekdays; Sat 7th). Dept D1: full-month employee, mid-month joiner, future joiner."""
    mk_emp(client, "EMP1001", dept="D1", joined="2026-01-01")
    mk_emp(client, "EMP1002", dept="D1", joined="2026-03-16")
    mk_emp(client, "EMP1003", dept="D1", joined="2026-04-01")
    put_log(db, "EMP1001", "2026-03-02", late=20, ot=40, wh=9.0)
    put_log(db, "EMP1001", "2026-03-03", status="WFH", wh=4.0, half=True)
    put_log(db, "EMP1001", "2026-03-04", status="ON_DUTY", wh=8.0)
    put_log(db, "EMP1001", "2026-03-05", status="LEAVE")
    put_log(db, "EMP1001", "2026-03-06", status="ABSENT")
    put_log(db, "EMP1001", "2026-03-07", late=15, ot=30, wh=5.0)       # Saturday
    put_log(db, "EMP1001", "2026-02-27", late=99, wh=9.0)               # other month, ignored


def test_employee_monthly(client, march):
    r = client.get("/analytics/employees/EMP1001/monthly", params=dict(month="2026-03")).json()
    assert r == {"emp_code": "EMP1001", "month": "2026-03", "working_days": 22, "present_days": 2.5, "leave_days": 1,
                 "late_count": 2, "total_late_minutes": 35, "total_overtime_minutes": 70, "attendance_pct": 11.36}
    r = client.get("/analytics/employees/EMP1002/monthly", params=dict(month="2026-03")).json()
    assert r["working_days"] == 12 and r["present_days"] == 0 and r["attendance_pct"] == 0.0 and r["late_count"] == 0
    r = client.get("/analytics/employees/EMP1003/monthly", params=dict(month="2026-03")).json()
    assert r["working_days"] == 0 and r["attendance_pct"] is None
    assert client.get("/analytics/employees/NOPE/monthly", params=dict(month="2026-03")).status_code == 404
    for bad in ("2026-13", "2026-3", "x", ""):
        assert client.get("/analytics/employees/EMP1001/monthly", params=dict(month=bad)).status_code == 422
    assert client.get("/analytics/employees/EMP1001/monthly").status_code == 422


def test_employee_monthly_pct_half_up(client, db):
    mk_emp(client, "EMP1010", dept="D1x", joined="2026-01-01")
    for d in (2, 3, 4, 5):                     # 4 full days of 22 -> 18.1818...
        put_log(db, "EMP1010", f"2026-03-{d:02d}", wh=8.0)
    assert client.get("/analytics/employees/EMP1010/monthly", params=dict(month="2026-03")).json()["attendance_pct"] == 18.18
    mk_emp(client, "EMP1011", dept="D1x", joined="2026-03-10")  # 16 working days; 0.5 day -> exactly 3.125
    put_log(db, "EMP1011", "2026-03-10", wh=3.0, half=True)
    r = client.get("/analytics/employees/EMP1011/monthly", params=dict(month="2026-03")).json()
    assert r["working_days"] == 16 and r["attendance_pct"] == 3.13   # half-up; banker's rounding would give 3.12


def test_department_summary(client, march):
    r = client.get("/analytics/departments/summary", params=dict(month="2026-03", department="D1")).json()
    assert r["month"] == "2026-03"
    assert r["items"] == [{"department": "D1", "headcount": 2, "present_days": 2.5, "avg_work_hours": 6.5, "late_count": 2,
                           "total_late_minutes": 35, "leave_count": 1, "on_duty_count": 1}]
    names = [i["department"] for i in client.get("/analytics/departments/summary", params=dict(month="2026-03")).json()["items"]]
    assert names == sorted(names) and "D1" in names
    # month before anyone joined: department omitted
    assert client.get("/analytics/departments/summary", params=dict(month="2025-12", department="D1")).json()["items"] == []
    # headcount counts a person with zero logs; avg_work_hours is null without hours
    r = client.get("/analytics/departments/summary", params=dict(month="2026-04", department="D1")).json()["items"][0]
    assert r["headcount"] == 3 and r["avg_work_hours"] is None and r["present_days"] == 0
    assert client.get("/analytics/departments/summary").status_code == 422


def test_department_summary_average_is_over_records(client, db):
    mk_emp(client, "EMP1020", dept="AvgD", joined="2026-01-01")
    mk_emp(client, "EMP1021", dept="AvgD", joined="2026-01-01")
    for d in (2, 3, 4, 5):
        put_log(db, "EMP1020", f"2026-03-{d:02d}", wh=10.0)
    put_log(db, "EMP1021", "2026-03-02", wh=1.0)
    put_log(db, "EMP1021", "2026-03-03", status="WFH", wh=None)   # open record: no hours, not in the mean
    put_log(db, "EMP1021", "2026-03-04", status="LEAVE", wh=7.0)  # not present-status: not in the mean
    r = client.get("/analytics/departments/summary", params=dict(month="2026-03", department="AvgD")).json()["items"][0]
    assert r["avg_work_hours"] == 8.2   # (40 + 1) / 5, not the mean of per-employee means (5.5)


@pytest.fixture(scope="module")
def late_board(client, db):
    for i, (dept, total) in enumerate([("LB1", 50), ("LB1", 40), ("LB2", 40), ("LB1", 30), ("LB2", 10), ("LB1", 0)]):
        code = f"EMP20{i:02d}"
        mk_emp(client, code, dept=dept)
        if total:
            put_log(db, code, "2026-05-04", late=total - 5)
            put_log(db, code, "2026-05-09", late=5)           # Saturday counts for lateness
        else:
            put_log(db, code, "2026-05-04", late=0)
    put_log(db, "GHOST1", "2026-05-04", late=500)               # no such employee


def test_late_leaderboard_ties(client, late_board):
    r = client.get("/analytics/leaderboard/late", params=dict(month="2026-05", limit=50)).json()["items"]
    mine = [(i["rank"], i["emp_code"], i["total_late_minutes"], i["late_count"]) for i in r if i["emp_code"].startswith("EMP20")]
    assert mine == [(1, "EMP2000", 50, 2), (2, "EMP2001", 40, 2), (2, "EMP2002", 40, 2), (4, "EMP2003", 30, 2), (5, "EMP2004", 10, 2)]
    assert all(i["emp_code"] not in ("GHOST1", "EMP2005") for i in r)
    assert set(r[0]) == {"rank", "emp_code", "name", "department", "total_late_minutes", "late_count"}
    lb = lambda n: [i["rank"] for i in client.get("/analytics/leaderboard/late", params=dict(month="2026-05", limit=n)).json()["items"]]
    assert lb(1) == [1]
    assert lb(2) == [1, 2, 2]     # limit applies after ranking: both tied rank-2 rows come back
    assert lb(3) == [1, 2, 2]
    assert lb(4) == [1, 2, 2, 4]
    assert len(client.get("/analytics/leaderboard/late", params=dict(month="2026-05")).json()["items"]) <= 10


def test_late_leaderboard_department_and_validation(client, late_board):
    r = client.get("/analytics/leaderboard/late", params=dict(month="2026-05", department="LB2")).json()["items"]
    assert [(i["rank"], i["emp_code"]) for i in r] == [(1, "EMP2002"), (2, "EMP2004")]   # ranked inside the department
    assert client.get("/analytics/leaderboard/late", params=dict(month="2026-05", limit=0)).status_code == 422
    assert client.get("/analytics/leaderboard/late", params=dict(month="2026-05", limit=51)).status_code == 422
    assert client.get("/analytics/leaderboard/late").status_code == 422
    assert client.get("/analytics/leaderboard/late", params=dict(month="2030-01")).json() == {"month": "2030-01", "items": []}


@pytest.fixture(scope="module")
def trend(client, db):
    mk_emp(client, "EMP3001", dept="TR", joined="2026-03-01")
    mk_emp(client, "EMP3002", dept="TR", joined="2026-03-04")
    put_log(db, "EMP3001", "2026-03-02", wh=8.0)
    put_log(db, "EMP3001", "2026-03-03", wh=4.0, half=True, late=30)
    put_log(db, "EMP3001", "2026-03-04", wh=8.0)
    put_log(db, "EMP3002", "2026-03-04", status="WFH", wh=8.0)
    put_log(db, "EMP3001", "2026-03-07", wh=8.0, late=12)   # Saturday


def test_department_trend(client, trend):
    r = client.get("/analytics/departments/TR/trend", params={"from": "2026-03-02", "to": "2026-03-10"}).json()
    assert r["department"] == "TR" and len(r["items"]) == 9
    rows = {i["date"]: i for i in r["items"]}
    assert list(rows) == [f"2026-03-{d:02d}" for d in range(2, 11)]
    assert [rows[d]["attendance_rate"] for d in rows] == [1.0, 0.5, 1.0, 0.0, 0.0, None, None, 0.0, 0.0]
    assert [rows[d]["moving_avg_7d"] for d in rows] == [1.0, 0.75, 0.8333, 0.625, 0.5, 0.5, 0.5, 0.3, 0.2]
    assert [rows[d]["headcount"] for d in rows] == [1, 1, 2, 2, 2, 2, 2, 2, 2]
    assert [rows[d]["is_working_day"] for d in rows] == [True] * 5 + [False] * 2 + [True] * 2
    assert rows["2026-03-07"]["present_count"] == 1.0 and rows["2026-03-07"]["late_count"] == 1
    assert rows["2026-03-03"]["present_count"] == 0.5 and rows["2026-03-03"]["late_count"] == 1
    assert rows["2026-03-04"]["present_count"] == 2.0
    assert set(r["items"][0]) == {"date", "is_working_day", "headcount", "present_count", "late_count", "attendance_rate", "moving_avg_7d"}


def test_department_trend_edges(client, trend):
    f = lambda **p: client.get("/analytics/departments/TR/trend", params=p)
    one = f(**{"from": "2026-03-07", "to": "2026-03-07"}).json()["items"]          # single weekend day
    assert len(one) == 1 and one[0]["attendance_rate"] is None and one[0]["moving_avg_7d"] is None
    before = f(**{"from": "2026-02-27", "to": "2026-03-02"}).json()["items"]      # before one employee joined
    assert [i["headcount"] for i in before] == [0, 0, 1, 1]
    empty = f(**{"from": "2020-01-01", "to": "2020-01-03"}).json()["items"]        # before anyone joined, no logs
    assert all(i["headcount"] == 0 and i["attendance_rate"] is None and i["moving_avg_7d"] is None for i in empty)
    assert len(f(**{"from": "2026-03-01", "to": "2026-05-31"}).json()["items"]) == 92
    assert f(**{"from": "2026-03-01", "to": "2026-06-01"}).status_code == 422
    assert f(**{"from": "2026-03-05", "to": "2026-03-04"}).status_code == 422
    assert f(**{"from": "2026-03-05"}).status_code == 422
    assert f(**{"from": "x", "to": "2026-03-05"}).status_code == 422
    assert client.get("/analytics/departments/NOPE/trend", params={"from": "2026-03-01", "to": "2026-03-02"}).status_code == 404


def test_trend_rate_rounds_half_up(client, db):
    for i in range(32):
        mk_emp(client, f"EMP31{i:02d}", dept="Big32", joined="2026-01-01")
    put_log(db, "EMP3100", "2026-03-02", wh=8.0)   # 1/32 = 0.03125 -> 0.0313 (banker's rounding would give 0.0312)
    r = client.get("/analytics/departments/Big32/trend", params={"from": "2026-03-02", "to": "2026-03-02"}).json()["items"][0]
    assert r["attendance_rate"] == 0.0313 and r["moving_avg_7d"] == 0.0313


# ------------------------------- explain ------------------------------------
@pytest.fixture(scope="module")
def big(client, db):
    """100k attendance records: 1000 employees x 100 days."""
    emps = [dict(emp_code=f"EMP{50000 + i}", name=f"B{i}", email="b@x.co", department=f"BD{i % 5}", shift_start="09:30",
                 shift_end="18:30", joined_on="2026-01-01", created_at=datetime.now(timezone.utc)) for i in range(1000)]
    db.employees.insert_many(emps)
    base = datetime(2026, 1, 1)
    logs = []
    for day in range(100):
        d = (base + timedelta(days=day)).date().isoformat()
        for i in range(1000):
            logs.append(dict(emp_code=f"EMP{50000 + i}", date=d, status="PRESENT" if (i + day) % 7 else "WFH",
                             punch_in=datetime(2026, 1, 1, 4, tzinfo=timezone.utc), punch_out=None, work_hours=None,
                             late_minutes=(i * day) % 40, overtime_minutes=0, half_day=False, history=[]))
    db.attendance_logs.insert_many(logs)
    return len(logs)


EXPLAIN_CASES = [
    ("attendance_list", dict(date_from="2026-02-01", date_to="2026-02-10")),
    ("attendance_list", dict()),
    ("attendance_list", dict(emp_code="EMP50010", page=2, page_size=10)),
    ("attendance_list", dict(status="WFH", date_from="2026-02-01", page=3)),
    ("employee_monthly", dict(emp_code="EMP50010", month="2026-02")),
    ("department_summary", dict(month="2026-02")),
    ("department_summary", dict(month="2026-02", department="BD1")),
    ("late_leaderboard", dict(month="2026-02", limit=5)),
    ("late_leaderboard", dict(month="2026-02", department="BD2")),
    ("department_trend", {"department": "BD3", "from": "2026-02-01", "to": "2026-03-31"}),
]


@pytest.mark.parametrize("endpoint,params", EXPLAIN_CASES)
def test_explain_uses_indexes(client, big, endpoint, params):
    r = client.get(f"/admin/explain/{endpoint}", params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["endpoint"] == endpoint and body["collection"] in ("attendance_logs", "employees")
    assert "executionStats" in r.text
    assert "IXSCAN" in r.text, r.text[:2000]
    assert "COLLSCAN" not in r.text, r.text[:2000]


def test_explain_validation(client):
    assert client.get("/admin/explain/nope").status_code == 422
    assert client.get("/admin/explain/employee_monthly", params=dict(month="2026-02")).status_code == 422
    assert client.get("/admin/explain/department_summary").status_code == 422
    assert client.get("/admin/explain/department_trend", params=dict(department="x")).status_code == 422
    assert client.get("/admin/explain/late_leaderboard", params=dict(month="2026-13")).status_code == 422


def test_big_dataset_endpoints_answer_correctly(client, big):
    assert big == 100_000
    r = client.get("/attendance", params=dict(date_from="2026-02-01", date_to="2026-02-02", page_size=100)).json()
    assert r["total"] == 2000 and len(r["items"]) == 100
    s = client.get("/analytics/departments/summary", params=dict(month="2026-02", department="BD1")).json()["items"][0]
    assert s["headcount"] == 200
    lb = client.get("/analytics/leaderboard/late", params=dict(month="2026-02", limit=3, department="BD1")).json()["items"]
    assert lb and lb[0]["rank"] == 1
