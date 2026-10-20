@echo off
cd /d "%~dp0"
if not exist .venv ( py -3 -m venv .venv && .venv\Scripts\pip install -q -r requirements.txt )
.venv\Scripts\uvicorn app.server:app --host 127.0.0.1 --port 8000
