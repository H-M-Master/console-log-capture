@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0switch-files.ps1" %*
set "code=%ERRORLEVEL%"
echo.
if not "%code%"=="0" pause
exit /b %code%
