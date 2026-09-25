@echo off
setlocal
cd /d "%~dp0.."
if not exist "logs" mkdir "logs"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PY=%PYTHON_EXE%"
if not defined PY set "PY=C:\Users\junquan\AppData\Local\Programs\Python\Python313\python.exe"
"%PY%" daily_digest.py %* >> "logs\daily_digest.log" 2>&1
exit /b %ERRORLEVEL%
