@echo off
setlocal
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not exist "%PYTHON%" set "PYTHON=%LocalAppData%\Programs\Python\Python310-32\pythonw.exe"
if not exist "%PYTHON%" (
  echo Python 3.10 was not found in the standard per-user install locations.
  echo Install Python 3.10 or edit this launcher.
  pause
  exit /b 1
)
start "AnyTestTools File Switcher" "%PYTHON%" "%~dp0app.py"
