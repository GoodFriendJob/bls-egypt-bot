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
REM
REM  NOTE for whoever edits this: do NOT try to read values out
REM  of config.yaml with a PowerShell one-liner here. A pipe
REM  character is parsed by cmd before PowerShell sees it, which
REM  is what produced "The syntax of the command is incorrect".
REM  The server prints its own address on startup; enough.
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

echo   Starting the dashboard. The address is printed below.
echo.
echo   To reach it from another computer, set in config.yaml:
echo       dashboard.host:     0.0.0.0
echo       dashboard.password: your-password
echo   then open  http://THIS-PC-IP:5000  from the other machine.
echo.
echo   Close this window to stop the dashboard. The bot keeps running.
echo.
echo -----------------------------------------------------------
echo.

REM Call the venv's python DIRECTLY rather than activating it first.
REM activate.bat bakes in the venv's ABSOLUTE path, so moving or renaming
REM this folder leaves it pointing somewhere that no longer exists; PATH
REM then falls through to the system python and the bot dies on
REM "No module named 'yaml'". python.exe finds its own pyvenv.cfg relative
REM to itself, so called by path it keeps working wherever the folder is.

".venv\Scripts\python.exe" -m dashboard.server

echo.
echo -----------------------------------------------------------
echo The dashboard has stopped. The bot is unaffected.

:end
echo.
pause
endlocal
