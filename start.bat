@echo off
chcp 65001 >nul
title Orbital
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 server.py
  goto end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
  goto end
)
echo.
echo Python was not found.
echo Install it once: open a command prompt and enter:
echo     winget install Python.Python.3.13
echo Then close this window and run start.bat again.
:end
echo.
pause
