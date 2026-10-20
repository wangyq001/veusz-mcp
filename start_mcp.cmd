@echo off
setlocal
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" "%~dp0veusz_mcp.py"
