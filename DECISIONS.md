# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` to enforce employee identity, and a unique compound index on `(emp_code, date)` to prevent duplicate daily attendance even when requests arrive together. The date/employee index supports the attendance list's descending date order, while date/status/employee supports filtered monthly analytics. I also index department and join date for headcount-oriented lookups. I would review actual execution plans before adding more indexes because each index costs storage and slows writes.

2. **Punch-in race.** Both requests may initially see no record, but the unique compound index allows only one insert. MongoDB raises a duplicate-key error for the loser, which the API translates to HTTP 409; the winner gets 201.

3. **Ties.** `$rank` gives equal totals equal ranks and skips the next rank. I filter rank after ranking, so ties at the limit are all returned; `emp_code` makes ordering deterministic.

4. **Headcount.** The department summary starts from eligible employees and looks up their logs, preserving employees with no matching records. Employees joining after month-end are excluded.

5. **100x data.** I would inspect explain statistics, add only proven indexes, consider pre-aggregated daily summaries, and load-test concurrent writes and analytics.
