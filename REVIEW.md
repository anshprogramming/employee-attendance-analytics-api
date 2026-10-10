# REVIEW.md

| # | Where | What is wrong | How you'd notice it | How fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses naive local datetime arithmetic and truncates by `int`, while instants are UTC and grace is strictly more than 10 minutes. | Punch at 09:40:01 IST should be 10 late minutes. | Use IST shift boundaries, UTC instants, second truncation, and whole-minute floor. |
| 2 | `compute_overtime` | Builds a timestamp with an inconsistent timezone and ignores overnight shift end dates and the 30-minute threshold. | Overnight shift 22:00–06:00 and a 20-minute overrun. | Compute the shift end on the correct calendar day and apply the >=30-minute rule. |
| 3 | `create_employee` | Check-then-insert permits concurrent duplicate employee codes; `created_at` is naive and returned as a datetime. | Parallel POST requests can create duplicates. | Unique database index, catch duplicate-key error, UTC time, epoch-ms response. |
| 4 | `list_employees` | `skip = page * page_size` skips page one; total ignores department filter; no validation/cap for pagination; no stable sort. | Page 1 starts after first page and filtered total is wrong. | `(page-1)*page_size`, filtered count, bounds and sort by employee code. |
| 5 | `punch_in` | Does not check unknown employee before dereferencing; accepts invalid statuses; local timestamp and no millisecond validation; check-then-insert race. | Unknown employee yields 500; parallel punch-ins can duplicate. | Validate request, normalize UTC instants and rely on unique `(emp_code,date)` index. |
| 6 | Punch-in date calculation | Uses UTC/local host date rather than IST and ignores overnight shift start date. | Punch at 02:00 IST for a 22:00–06:00 shift is assigned to the wrong day. | Derive attendance day using employee shift and IST. |
| 7 | `list_attendance` | Loads all matching records into Python and sorts/slices in memory; pagination and count become slow at scale. | Large collection causes high memory and latency. | MongoDB sort/skip/limit and filtered `count_documents`. |
| 8 | `list_attendance` | Response includes MongoDB `_id` unless transformed and nested BSON datetimes are not converted to epoch milliseconds. | Contract response leaks internal IDs or returns wrong timestamp types. | Serialize response at the API boundary. |
| 9 | Punch-out (missing) | Endpoint is absent. | Contract route returns 404. | Added atomic conditional update and computed fields. |
| 10 | Regularization (missing) | Endpoint is absent; no validation or audit history. | Contract route returns 404. | Added validation, derived-field recomputation, changed-fields-only history, and compare-and-set conflict handling. |
| 11 | Analytics (missing) | Required endpoints are absent. | Contract routes return 404. | Added aggregation-backed monthly, department, leaderboard and trend endpoints. |
| 12 | `/health` | Starter returns success without checking MongoDB readiness. | Stop MongoDB and health still reports ready. | Ping MongoDB and return 503 on failure. |
| 13 | Stored timestamps | Starter mixes naive local datetimes with UTC expectations and does not truncate to seconds. | Host timezone changes behaviour; fractional seconds leak into storage. | Normalize all instants to UTC and whole seconds. |
| 14 | Audit writes | A read-modify-write history update can lose a concurrent correction. | Parallel PATCH requests overwrite one another's history. | Conditional update detects concurrent change and returns 409. |

## Examined and retained

- Keeping `emp_code` as the public identity is correct; MongoDB `_id` remains internal.
- `history` should contain manual corrections only; punch-in and punch-out do not append history.
- Analytics must use stored derived values rather than recomputing from timestamps, because seeded derived values are authoritative.
