# desktop

Windows app (Python 3.12 + PySide6) for ingest, triage, dedupe, cleanup, faces, and sync.
Runs on George's laptop; talks to the local Postgres and the Mac mini inference service.
Never writes to `D:\Photos` or `D:\Scanned Photos` — those are read-only masters.

## Setup

```
py -3.12 -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env
# edit .env — set DATABASE_URL, INFERENCE_TOKEN, WEB_API_TOKEN
python -m photoarchive
```

`python -m photoarchive --smoke` opens the window and self-closes after 1 second (used by CI/smoke checks).
