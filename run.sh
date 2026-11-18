#!/usr/bin/env bash
# PRESFOR - version locale (poste du contrôleur) sur http://127.0.0.1:8000
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.local.example .env
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
export LOCAL_MODE=1 DATA_DIR="$PWD/donnees" CACHE_DIR="$PWD/donnees/cache" JOBS_DIR="$PWD/donnees/jobs"
exec .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
