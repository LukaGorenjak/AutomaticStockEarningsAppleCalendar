@echo off
setlocal
cd /d "%~dp0\.."

set NO_PAUSE=0
if /i "%~1"=="--no-pause" (
    set NO_PAUSE=1
    shift
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found at .venv\Scripts\python.exe
    if "%NO_PAUSE%"=="0" pause
    exit /b 1
)

".venv\Scripts\python.exe" -m earnings_cal.sync_excel --build --push %1 %2 %3 %4 %5
set EXIT_CODE=%ERRORLEVEL%

if "%NO_PAUSE%"=="0" (
    echo.
    pause
)

exit /b %EXIT_CODE%
