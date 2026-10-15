# Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the application. **All** application code is in this one file. It starts with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md` (defects found in the starter), `DECISIONS.md` (design answers).

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Project Overview

**Employee Attendance & Analytics API** is a backend service for employee attendance tracking, attendance corrections with an
audit trail, and HR analytics. It has no user interface; it is a JSON HTTP API that follows `openapi.yaml`.

**Stack:** Python 3.11+, FastAPI (with Pydantic for validation), PyMongo, MongoDB 6.0+. Tests use pytest and httpx.

**Features**
- Employee management: create (unique `emp_code`) and list with a department filter and pagination.
- Punch-in and punch-out with the business rules applied: IST attendance dates, overnight shifts, 10-minute lateness grace,
  overtime from 30 minutes, work hours rounded half-up, half-day detection.
- Attendance listing with filters (employee, dates, status), sorting and pagination.
- Manual corrections (`PATCH`) that recompute the derived fields and append a before/after entry to an append-only `history`.
- Analytics: monthly summary per employee, department summary (headcount includes employees with no logs), late-comer
  leaderboard with ties (ranks 1, 2, 2, 4), and a department trend with one row per calendar day and a 7-day moving average.
- `GET /admin/explain/{endpoint}` returns MongoDB `executionStats` for the real query or pipeline of each list/analytics endpoint.
- Indexes are created at startup. Concurrency safeguards: unique indexes decide duplicate creates and punch-ins, punch-out is a
  single conditional update, and a correction applies only if the record is unchanged since it was read (otherwise 409).

**Architecture.** Everything is in `app/main.py`. FastAPI routes validate input (a bad body, date, month or timestamp is a 422) and
call small helper functions for the rules (R1-R5). Instants are epoch milliseconds in the API and UTC datetimes in MongoDB;
dates, months and shift times are plain strings. The analytics endpoints are MongoDB aggregation pipelines (`$lookup`,
`$group`, `$setWindowFields` with `$rank` and a 7-row window, `$range` for gap filling); Python only does final half-up rounding of
the returned totals and counts weekdays for `working_days`. The analytics read the stored derived values and do not recompute them.

## Verification status
- **Current audit, MongoDB 8.2.5 (local):** `python -m pytest tests -q` gave **48 passed**, and the app starts and `/health` returns 200.
  A separate randomized comparison of the analytics endpoints against a brute-force Python reference found no mismatches
  (a throwaway script, not part of `tests/`).
- **Not verified:** MongoDB 7.0 (the grader's version; the pipeline stages used exist since 5.0), Python 3.11, the hidden dataset,
  and the oldest allowed dependency versions. Explain plans were checked for `IXSCAN` and no `COLLSCAN` only on a generated
  100 000-record set. The moving average takes the mean of the reported (4-decimal) `attendance_rate` values, which is one
  reading of the contract wording.

## Configuration
| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string (local, Docker or Atlas) |
| `MONGO_DB` | `attendance_db` | Database name |

Real environment variables win over a local `.env` file (which is optional and must not be committed).
MongoDB **6.0 or newer** is required (`$setWindowFields`, `$dateAdd`, `$getField`); it was developed against 8.2 and the grader uses 7.0.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # local docker: docker run -p 27017:27017 mongo:7
python sample_seed.py                                   # optional: loads the 9 sample documents (it empties both collections)
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup (idempotent); there is no manual database setup.
Interactive docs are at `http://localhost:8000/docs`.

## Tests
```bash
pip install pytest httpx   # test-only dependencies (httpx is used by FastAPI's TestClient), not needed to run the app
python -m pytest tests -q
```
The tests need a running MongoDB (`MONGO_URI`, default `mongodb://localhost:27017`). They always use the database
`attendance_test` (they ignore `MONGO_DB`) and drop it before and after the run. They cover R1-R10 boundaries, validation,
overnight shifts, pagination, the three aggregations (ties, no-log employees, mid-month joiners, gap filling, moving average),
concurrent punch-in / punch-out / create / regularize, and run `/admin/explain/*` against a generated 100 000-record collection,
asserting `IXSCAN` and no `COLLSCAN`.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile.
