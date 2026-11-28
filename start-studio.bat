@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" run.py studio %*
  exit /b %errorlevel%
)
where uv >nul 2>nul
if not errorlevel 1 (
  uv run python run.py studio %*
  exit /b %errorlevel%
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3.11 run.py studio %*
  exit /b %errorlevel%
)
echo Python 3.11+ or uv is required. 1>&2
exit /b 1
