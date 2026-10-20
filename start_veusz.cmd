@echo off
setlocal
set "task_python=%~dp0.venv\Scripts\python.exe"
if not exist "%task_python%" (
    echo Project Python environment not found. See README.md.
    pause
    exit /b 1
)
cd /d "%~dp0"
"%task_python%" bridge_cli.py launch %*
if errorlevel 1 pause
