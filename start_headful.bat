@echo off
REM ===========================================================
REM  BLS Appointment Bot - Test run (visible browser, one check)
REM
REM  Use this to SEE what the bot sees. It opens a real browser
REM  window, does ONE check, then stops.
REM ===========================================================
setlocal
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Test run (debugging)
echo ===========================================================
echo.
echo This will:
echo   - open a browser window you can watch
echo   - log in and check for appointments ONE time
echo   - then stop by itself
echo.

REM --- Check the virtual environment --------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] The bot is not installed yet.
    echo.
    echo Please run  install.bat  first, then try again.
    goto :end
)

REM --- Check the config file ----------------------------------
if not exist "config.yaml" (
    echo [ERROR] The file config.yaml is missing.
    echo.
    echo Please run  install.bat  first, then open config.yaml
    echo and fill in your email, password and Telegram details.
    goto :end
)

REM --- Is the bot already running? -----------------------------
REM  Two bots at the same time can lock each other out of the
REM  BLS account, so stop the running one first.
call :count_bot
if %BOTCOUNT% GTR 0 (
    echo The bot is ALREADY RUNNING.
    echo Number of bot processes found: %BOTCOUNT%
    echo.
    echo Please run  stop.bat  first, then run this test again.
    echo Two bots at the same time can lock your BLS account.
    goto :end
)

echo -----------------------------------------------------------
echo.

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Could not activate the virtual environment.
    echo Please run install.bat again.
    goto :end
)

python bot.py --headful --once

echo.
echo -----------------------------------------------------------
echo The test run has finished.
echo.
echo If something went wrong, look here:
echo   - pictures of the page:  logs\screenshots
echo   - page code dumps:       logs\dom
echo   - full text log:         logs\bot.log
echo.
echo If you saw "403 Forbidden": the BLS website refused your
echo internet connection. It only answers connections from
echo inside Egypt. Connect from Egypt (or an Egyptian VPS / VPN)
echo and try again.
goto :end

REM --- Helper: count running bot processes ---------------------
REM  No pipe characters allowed in the PowerShell below - see start.bat for why.
:count_bot
set "BOTCOUNT=0"
for /f %%A in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=Get-CimInstance Win32_Process; $m=$a.Where({$_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*'}); $ids=@($m.ProcessId); $t=$m.Where({$ids -notcontains $_.ParentProcessId}); @($t).Count" 2^>nul') do set "BOTCOUNT=%%A"
if not defined BOTCOUNT set "BOTCOUNT=0"
exit /b

:end
echo.
pause
endlocal
