@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title Visual DPS Local Server
set "PORT=8767"
set "APP_URL=http://127.0.0.1:%PORT%/"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

echo.
echo ========================================
echo   Visual DPS Local Server
echo   Project: %CD%
echo   URL:     %APP_URL%
echo ========================================
echo.

if exist ".venv\Scripts\python.exe" goto python_ready
echo [ERROR] Project Python environment was not found:
echo         %CD%\.venv\Scripts\python.exe
echo.
echo Create the environment first:
echo   py -3.10 -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
if defined VISUAL_DPS_NO_BROWSER exit /b 1
pause
exit /b 1

:python_ready
".venv\Scripts\python.exe" -c "import fastapi, uvicorn" >nul 2>&1
if not errorlevel 1 goto dependencies_ready
echo [ERROR] FastAPI or Uvicorn is missing from .venv.
echo Run:
echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
echo.
if defined VISUAL_DPS_NO_BROWSER exit /b 1
pause
exit /b 1

:dependencies_ready
powershell.exe -NoProfile -Command "$p=%PORT%; if (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 goto start_server

echo [INFO] Port %PORT% is already running. Opening the existing page.
if defined VISUAL_DPS_NO_BROWSER exit /b 0
start "" "%APP_URL%"
echo.
pause
exit /b 0

:start_server
echo [START] Starting with the project .venv ...
echo [INFO] The browser will open in about 3 seconds.
echo [STOP] Press Ctrl+C in this window, then enter Y.
echo.

if defined VISUAL_DPS_NO_BROWSER goto run_server
start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process '%APP_URL%'"

:run_server
".venv\Scripts\python.exe" server.py --host 127.0.0.1 --port %PORT%
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" goto server_failed
echo [DONE] Local server stopped.
goto finish

:server_failed
echo [ERROR] Server exited with code %EXIT_CODE%.
echo Keep the error messages above for troubleshooting.

:finish
echo.
if defined VISUAL_DPS_NO_BROWSER exit /b %EXIT_CODE%
pause
exit /b %EXIT_CODE%
