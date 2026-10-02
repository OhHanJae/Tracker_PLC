@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_BIN="
set "PYTHON_ARG="

if defined PYTHON call :try_python "%PYTHON%"
if not defined PYTHON_BIN call :try_python "py" "-3"
if not defined PYTHON_BIN call :try_python "python"
if not defined PYTHON_BIN if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" call :try_python "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"

if not defined PYTHON_BIN (
    echo [ERROR] Python 3.10 or later was not found.
    exit /b 1
)

echo Starting XGT Gateway...
"%PYTHON_BIN%" %PYTHON_ARG% gateway.py %*
exit /b %errorlevel%

:fail
echo.
echo [ERROR] Gateway setup or launch failed.
exit /b 1

:try_python
set "CANDIDATE=%~1"
set "CANDIDATE_ARG=%~2"
if "%CANDIDATE_ARG%"=="" (
    "%CANDIDATE%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
) else (
    "%CANDIDATE%" "%CANDIDATE_ARG%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>nul
)
if not errorlevel 1 (
    set "PYTHON_BIN=%CANDIDATE%"
    set "PYTHON_ARG=%CANDIDATE_ARG%"
)
exit /b 0
