#!/usr/bin/env bash
# Lance l'application sur http://127.0.0.1:8000 (poste local uniquement)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
exec .venv/bin/uvicorn app.server:app --host 127.0.0.1 --port 8000
