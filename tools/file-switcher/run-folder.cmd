@echo off
setlocal EnableExtensions
set "HERE=%~dp0"
set "PYTHON=%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
if not exist "%PYTHON%" (
  for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do if not defined PYTHON_FROM_PATH set "PYTHON_FROM_PATH=%%P"
  if defined PYTHON_FROM_PATH set "PYTHON=%PYTHON_FROM_PATH%"
)
if not exist "%PYTHON%" (
  echo [Folder Switcher] Python was not found.
  echo Tried: %LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe and PATH:pythonw.exe
  echo Install Python 3.10+ and enable pythonw.exe, or edit this launcher.
  echo Tool directory: %HERE%
  pause
  exit /b 1
)
if not exist "%HERE%folder_app.py" (
  echo [Folder Switcher] Missing GUI: %HERE%folder_app.py
  pause
  exit /b 1
)
start "AnyTestTools Folder Switcher" "%PYTHON%" "%HERE%folder_app.py"
