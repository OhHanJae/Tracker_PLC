#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

python_bin="${PYTHON:-python3}"
venv_dir="${XGT_VENV:-.venv-linux}"
venv_python="$venv_dir/bin/python"

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "[ERROR] Python 3.10 or later was not found. Install python3 first."
  exit 1
fi

if ! "$python_bin" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
  echo "[ERROR] Python 3.10 or later is required."
  exit 1
fi

if [[ ! -x "$venv_python" ]]; then
  echo "[1/3] Creating virtual environment..."
  if ! "$python_bin" -m venv "$venv_dir"; then
    echo "[ERROR] Failed to create $venv_dir. On Ubuntu, install: sudo apt install python3-venv"
    exit 1
  fi
fi

if ! "$venv_python" -c 'import PySide6' >/dev/null 2>&1; then
  echo "[2/3] Installing PySide6 client dependency..."
  "$venv_python" -m pip install --upgrade pip
  "$venv_python" -m pip install -r requirements-client.txt
fi

echo "[3/3] Starting XGT Gateway Config..."
exec "$venv_python" config_client.py
