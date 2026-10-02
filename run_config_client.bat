@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python launcher "py" was not found.
    echo Install Python 3.10 or later and enable the Python launcher.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    py -3 -m venv .venv
    if errorlevel 1 goto :fail
)

call ".venv\Scripts\activate.bat"
python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
    echo [2/3] Installing PySide6 client dependency...
    python -m pip install --upgrade pip
    python -m pip install -r requirements-client.txt
    if errorlevel 1 goto :fail
)

echo [3/3] Starting XGT Gateway Config...
python config_client.py
if errorlevel 1 goto :fail
exit /b 0

:fail
echo.
echo [ERROR] Client setup or launch failed.
pause
exit /b 1
