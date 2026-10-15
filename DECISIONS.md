# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** `attendance_logs`: unique `(emp_code, date)`, which is the duplicate guard and serves per-employee queries; `(date desc, emp_code)` for the list sort and month-wide scans; `(status, date desc, emp_code)` for status filters. `employees`: unique `emp_code`, `(department, emp_code)` for paged lists, `(department, joined_on)` and `(joined_on)` for headcount. Rejected: a covered `(date, late_minutes, emp_code)` index for the leaderboard; it costs every write for one endpoint.
2. **Punch-in race.** Both requests call `insert_one` with no prior read. The unique index lets exactly one insert succeed; the other gets `DuplicateKeyError`, which becomes 409.
3. **Ties.** Ranking uses `$rank`, then `rank <= limit` is applied. Everyone tied at the cutoff is returned, so there can be more than `limit` rows, ordered by minutes descending, then `emp_code`.
4. **Headcount.** The summary starts from `employees` (joined by month end) and `$lookup`s logs. An employee with no logs gets an empty lookup, and `$unwind` with `preserveNullAndEmptyArrays` keeps them, so they still count.
5. **100x data.** Maintain pre-aggregated monthly rollups, updated on punch and correction, instead of scanning logs per request; the trend also packs a whole department into one document (16 MB limit).
