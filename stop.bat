@echo off
REM ===========================================================
REM  BLS Appointment Bot - Stop
REM
REM  Tries the polite way first (asks the bot to finish what it
REM  is doing and save its login session). Only forces the
REM  process to close if the polite way does not work.
REM ===========================================================
setlocal
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Stop
echo ===========================================================
echo.

REM --- Check the virtual environment --------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] The bot is not installed in this folder.
    echo There is nothing to stop here.
    echo.
    echo If you expected the bot to be installed, run install.bat.
    goto :end
)

REM --- Is anything running? ------------------------------------
call :count_bot
if %BOTCOUNT% EQU 0 (
    echo The bot is NOT running. Nothing to stop.
    goto :end
)

echo Found %BOTCOUNT% bot process^(es^) running.
echo.

REM --- Step 1: ask nicely through the dashboard ----------------
echo [1 of 3] Asking the bot to stop nicely ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $body = ConvertTo-Json -Compress -InputObject @{action='stop'}; $null = Invoke-RestMethod -Uri 'http://127.0.0.1:5000/api/control' -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 5; Write-Host '         The bot accepted the stop request.' } catch { Write-Host '         No answer from the dashboard - will close the process instead.' }"
echo.

REM --- Step 2: give it time to finish --------------------------
echo [2 of 3] Waiting up to 20 seconds for a clean shutdown ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "for ($i=0; $i -lt 20; $i++) { $a=Get-CimInstance Win32_Process; $m=$a.Where({$_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*'}); if (@($m).Count -eq 0) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1"
if not errorlevel 1 (
    echo.
    echo The bot stopped cleanly. Login session was saved.
    goto :done
)
echo         Still running.
echo.

REM --- Step 3: close the process -------------------------------
echo [3 of 3] Closing the bot process now ...
for /f %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "$a=Get-CimInstance Win32_Process; $m=$a.Where({$_.Name -like 'python*' -and $_.CommandLine -like '*bot.py*'}); foreach ($x in $m) { $x.ProcessId }" 2^>nul') do (
    echo         Stopping process ID %%P ...
    taskkill /PID %%P /T >nul 2>&1
    timeout /t 3 /nobreak >nul
    taskkill /F /PID %%P /T >nul 2>&1
)

REM --- Check the result ----------------------------------------
call :count_bot
if %BOTCOUNT% GTR 0 (
    echo.
    echo [WARNING] %BOTCOUNT% bot process^(es^) are still running.
    echo Please open Task Manager and end python.exe by hand.
    goto :end
)
echo.
echo The bot was closed.

:done
echo.
echo You can start it again any time with  start.bat
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
