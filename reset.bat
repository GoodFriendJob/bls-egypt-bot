@echo off
REM ===========================================================
REM  BLS Appointment Bot - Fresh start
REM
REM  Clears the saved login and the "portal blocked us" timer so
REM  the next start.bat begins immediately instead of waiting.
REM
REM  It does NOT touch config.yaml, your documents, or the logs.
REM ===========================================================
setlocal
REM %~dp0 = the folder this .bat lives in, so the path is never hardcoded.
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Fresh start
echo ===========================================================
echo.
echo Folder: %CD%
echo.

REM --- Refuse while the bot is running -------------------------
REM  Clearing the session under a live bot would have it log in again
REM  mid-cycle for no reason, and the cooldown it is waiting out is
REM  usually the very thing protecting the address from another block.
call :count_bot
if %BOTCOUNT% GTR 0 (
    echo [STOP] The bot is still running.
    echo.
    echo Stop it first ^(run stop.bat^), then run this again.
    goto :end
)

set "CLEARED=0"

REM --- The saved login ----------------------------------------
if exist "session\cookies.json" (
    del /q "session\cookies.json"
    echo   cleared: the saved login ^(session\cookies.json^)
    echo            the bot will sign in from scratch next time
    set "CLEARED=1"
) else (
    echo   nothing to clear: there is no saved login
)

REM --- The block timer -----------------------------------------
REM  WARNING worth understanding: this marker is how the bot avoids
REM  asking an address that is already being refused. Clearing it on
REM  the SAME connection that earned the block makes the block worse,
REM  not better. It is meant for a different computer or a different
REM  internet connection, where the timer does not apply at all.
if exist "logs\cooldown.json" (
    del /q "logs\cooldown.json"
    echo   cleared: the "portal blocked us" timer ^(logs\cooldown.json^)
    echo            the bot will try again straight away
    set "CLEARED=1"
) else (
    echo   nothing to clear: there is no block timer
)

echo.
if "%CLEARED%"=="1" (
    echo Done. Run start.bat to begin.
    echo.
    echo If the portal refused this connection recently, give it time
    echo anyway - starting again immediately is what makes a short
    echo block into a long one.
) else (
    echo Nothing needed clearing - start.bat will begin straight away.
)
goto :end

REM --- Helper: count running bot processes ---------------------
REM  NOTE for whoever edits this: the PowerShell below must contain NO pipe
REM  characters. It sits inside double quotes, so cmd does not treat a "|" as a
REM  pipe - writing "^|" passes a literal caret to PowerShell, which then errors
REM  and silently returns nothing (the count stays 0 and the check is useless).
REM  The .Where({...}) method form avoids pipes completely.
:count_bot
set "BOTCOUNT=0"
for /f %%C in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=Get-CimInstance Win32_Process; $m=$a.Where({$_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*'}); @($m).Count" 2^>nul') do set "BOTCOUNT=%%C"
exit /b

:end
echo.
pause
endlocal
