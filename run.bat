@echo off
rem PRESFOR - version locale (poste du controleur). Necessite Python 3.12 (python.org, cocher "Add to PATH").
cd /d "%~dp0"
if not exist .env copy .env.local.example .env >nul
if not exist .venv ( py -3 -m venv .venv && .venv\Scripts\pip install -q -r requirements.txt )
set LOCAL_MODE=1
set DATA_DIR=%~dp0donnees
set CACHE_DIR=%~dp0donnees\cache
set JOBS_DIR=%~dp0donnees\jobs
start "" http://127.0.0.1:8000
.venv\Scripts\uvicorn app.server:app --host 127.0.0.1 --port 8000
