@echo off
REM ===========================================================
REM  BLS Appointment Bot - Start (normal 24/7 run)
REM ===========================================================
setlocal
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Start
echo ===========================================================
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
call :count_bot
if %BOTCOUNT% GTR 0 (
    echo The bot is ALREADY RUNNING.
    echo Number of bot processes found: %BOTCOUNT%
    echo.
    echo You do not need to start it again.
    echo   - To see what it is doing:  http://127.0.0.1:5000
    echo   - To stop it:               run stop.bat
    goto :end
)

REM --- Start ---------------------------------------------------
echo Starting the bot ...
echo.
echo   Dashboard:  http://127.0.0.1:5000
echo   To stop:    press Ctrl+C here, or run stop.bat
echo.
echo -----------------------------------------------------------
echo.

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Could not activate the virtual environment.
    echo Please run install.bat again.
    goto :end
)

python bot.py

echo.
echo -----------------------------------------------------------
echo The bot has stopped.
goto :end

REM --- Helper: count running bot processes ---------------------
REM  NOTE for whoever edits this: the PowerShell below must contain NO pipe
REM  characters. It sits inside double quotes, so cmd does not treat a "|" as a
REM  pipe - writing "^|" passes a literal caret to PowerShell, which then errors
REM  and silently returns nothing (the count stays 0 and the check is useless).
REM  The .Where({...}) method form avoids pipes completely.
REM  Each bot also spawns one logging child process, so only processes whose
REM  parent is not itself a bot are counted - that is the real number of bots.
:count_bot
set "BOTCOUNT=0"
for /f %%A in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=Get-CimInstance Win32_Process; $m=$a.Where({$_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*'}); $ids=@($m.ProcessId); $t=$m.Where({$ids -notcontains $_.ParentProcessId}); @($t).Count" 2^>nul') do set "BOTCOUNT=%%A"
if not defined BOTCOUNT set "BOTCOUNT=0"
exit /b

:end
echo.
pause
endlocal
