@echo off
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "Optical Book Tool" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py"
) else (
  python "%~dp0run.py"
)
