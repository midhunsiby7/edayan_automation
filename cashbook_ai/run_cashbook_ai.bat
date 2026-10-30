@echo off
setlocal enabledelayedexpansion

title Cashbook AI - Parish Accounting Assistant
echo ========================================================================
echo                   CASHBOOK AI - LOCAL APPLICATION
echo ========================================================================
echo.
echo Initializing local environment and services...

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found in %~dp0.venv
    echo Please ensure the Python virtual environment is set up.
    pause
    exit /b 1
)

echo Checking backend dependencies...
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, openpyxl, PIL; print('[OK] Core packages verified.')"
if errorlevel 1 (
    echo [ERROR] Missing required packages in virtual environment.
    pause
    exit /b 1
)

echo.
echo ========================================================================
echo Starting local web server on http://127.0.0.1:8000
echo Security: Bound strictly to localhost (127.0.0.1)
echo ========================================================================
echo.
echo Launching default web browser...
start "" "http://127.0.0.1:8000"

echo.
echo Server logs (Press Ctrl+C to stop application):
echo ------------------------------------------------------------------------
".venv\Scripts\uvicorn.exe" src.app.main:app --host 127.0.0.1 --port 8000 --log-level info

pause
