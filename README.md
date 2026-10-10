# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the candidate assignment. It supports employee CRUD listing, punch-in/out, auditable manual corrections, monthly/department analytics, a late leaderboard, department trend, and explain output.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your shell (or in a local, uncommitted .env)
uvicorn app.main:app --port 8000 --reload
```

The API reads `MONGO_URI` and `MONGO_DB` from environment variables. Indexes are created idempotently at startup. Interactive API docs are at `/docs`. The assignment kit sample data is intentionally not included in this submission repository; load it separately when testing if needed.

## Notes

- All API instants are epoch milliseconds; MongoDB stores UTC BSON datetimes.
- Analytics are implemented with MongoDB aggregation pipelines.
- Review `REVIEW.md` and `DECISIONS.md` for code review and design notes.
- Before submission, run through the supplied contract and add tests for concurrent requests, overnight shifts, tie ranks, join dates, and zero-log employees. Verify all aggregation pipelines against MongoDB 7.0; aggregation stages such as `$densify` and `$setWindowFields` require a supported MongoDB version.
