@echo off
REM ===========================================================
REM  BLS Appointment Bot - Dashboard (runs on its own)
REM
REM  The dashboard is a SEPARATE program from the bot. Starting
REM  the bot no longer opens it, and closing this window does
REM  not stop the bot.
REM
REM  You can run this with the bot running or not running. With
REM  the bot stopped the page still opens and simply says the
REM  bot is offline.
REM ===========================================================
setlocal
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Dashboard
echo ===========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] The bot is not installed yet.
    echo.
    echo Please run  install.bat  first, then try again.
    goto :end
)

if not exist "config.yaml" (
    echo [ERROR] The file config.yaml is missing.
    echo.
    echo Please run  install.bat  first.
    goto :end
)

REM --- Show where it will be reachable -------------------------
for /f "usebackq delims=" %%H in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Select-String -Path config.yaml -Pattern '^\s*host:' | Select-Object -First 1).Line -replace '.*host:\s*', '' -replace '\"', '' -replace '#.*', ''"`) do set DASHHOST=%%H
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -Command "(Select-String -Path config.yaml -Pattern '^\s*port:' | Select-Object -First 1).Line -replace '.*port:\s*', '' -replace '\"', '' -replace '#.*', ''"`) do set DASHPORT=%%P

if "%DASHHOST%"=="" set DASHHOST=127.0.0.1
if "%DASHPORT%"=="" set DASHPORT=5000

echo   Address:  http://%DASHHOST%:%DASHPORT%
echo.
echo   To reach it from another computer, set in config.yaml:
echo       dashboard.host:     0.0.0.0
echo       dashboard.password: your-password
echo   then open  http://THIS-PC-IP:%DASHPORT%  from the other machine.
echo.
echo   Close this window to stop the dashboard. The bot keeps running.
echo.
echo -----------------------------------------------------------
echo.

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Could not activate the virtual environment.
    echo Please run install.bat again.
    goto :end
)

python -m dashboard.server

echo.
echo -----------------------------------------------------------
echo The dashboard has stopped. The bot is unaffected.

:end
echo.
pause
endlocal
