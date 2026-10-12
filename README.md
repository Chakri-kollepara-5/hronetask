# hronetask

HROne Employee Attendance & Analytics API, implemented with FastAPI, PyMongo, and MongoDB 6.0+.

## Project structure

- [app/main.py](app/main.py): all application code, endpoints, validation, indexing, aggregation, and startup logic.
- [openapi.yaml](openapi.yaml): source-of-truth API contract and business rules.
- [DATA_MODEL.md](DATA_MODEL.md): MongoDB shapes and stored-field semantics.

## Setup and run

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
uvicorn app.main:app --port 8000 --reload
```

The app reads `MONGO_URI` and `MONGO_DB` from the environment, with local defaults for MongoDB on localhost and the `attendance_db` database. Process environment values take precedence over `.env` values. `GET /health` returns 200 when MongoDB is reachable. Seed the database separately before exercising analytics; the service never seeds or clears data.

## Tests

```bash
python -m pytest -q
```

Unit tests use `mongomock` for request behavior and calculations supported by the test double. Real-Mongo integration tests cover aggregation semantics, MongoDB window operators, index constraints, concurrent writes, and execution-stat explain plans. They require MongoDB 6.0+ and run when `MONGO_TEST_URI` points to a disposable test server. Mock tests do not prove MongoDB locking, aggregation, or explain-plan behavior.

## Notes

- Startup creates required indexes idempotently and does not seed or modify existing records.
- Duplicate employee and attendance records are protected by unique indexes; punch-out and regularization use conditional updates.
- The API contract and business rules are defined in `openapi.yaml` and `DATA_MODEL.md`.
