# HROne Employee Attendance & Analytics API

This project implements the FastAPI employee attendance and analytics service described in the assignment contract.

## Project structure

- [app/main.py](app/main.py): all application code, endpoints, validation, indexing, aggregation, and startup logic.
- [openapi.yaml](openapi.yaml): source-of-truth API contract and business rules.
- [DATA_MODEL.md](DATA_MODEL.md): MongoDB shapes and stored-field semantics.
- [sample_seed.py](sample_seed.py): loads sample seed data for local validation.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI='mongodb://localhost:27017'
export MONGO_DB='attendance_db'
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

The app reads MONGO_URI and MONGO_DB from the environment; environment variables take precedence over any .env file.

## Notes

- Startup creates required MongoDB indexes idempotently without reseeding or modifying grader data.
- Punch-in and punch-out flows are implemented with atomic DB writes and duplicate-key handling for concurrent requests.
- Analytics are performed through MongoDB aggregation pipelines.
- The endpoint contract, validation rules, and status semantics follow openapi.yaml and the business rules in the assignment documents.

## Validation

A runtime smoke test was executed against an in-memory Mongo stub using FastAPI's TestClient.

Observed result:
- Employee creation: 201 and duplicate create: 409
- Punch-in success: 201 and duplicate same date: 409
- Health check: 200
- Attendance listing: 200
- Employee monthly summary: 200

This confirms the app boots and the main endpoints respond without runtime exceptions in the local validation environment.

# hronetask
