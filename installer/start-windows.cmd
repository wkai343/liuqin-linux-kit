@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
where uv.exe >nul 2>&1
if errorlevel 1 (
  echo Install uv first: https://docs.astral.sh/uv/getting-started/installation/
  pause
  exit /b 1
)
uv run --locked --project "%~dp0." "%~dp0install-gui.py" %*
set "liuqin_result=%ERRORLEVEL%"
if not "%liuqin_result%"=="0" pause
exit /b %liuqin_result%
