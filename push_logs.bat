@echo off
REM ===========================================================
REM  BLS Appointment Bot - Push run logs to git
REM
REM  Run this AFTER a bot run to upload the logs and screenshots
REM  so they can be reviewed on another computer.
REM ===========================================================
setlocal
REM %~dp0 = the folder this .bat lives in, so the path is never hardcoded.
cd /d "%~dp0"

echo ===========================================================
echo    Push run logs to git
echo ===========================================================
echo.
echo Folder: %CD%
echo.

REM --- Is git installed? ---------------------------------------
git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git is not installed, or not in PATH.
    echo Download it from  https://git-scm.com/download/win
    goto :end
)

REM --- Is this a git repository? -------------------------------
git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] This folder is not a git repository.
    echo.
    echo Set it up once with:
    echo     git init
    echo     git remote add origin YOUR_REPO_URL
    echo     git branch -M main
    goto :end
)

REM --- Anything to send? ---------------------------------------
if not exist "logs" (
    echo No logs folder yet. Run the bot first.
    goto :end
)

echo Adding log files ...
git add logs/
if errorlevel 1 (
    echo [ERROR] git add failed.
    goto :end
)

REM  git diff --cached --quiet returns 1 when something IS staged.
git diff --cached --quiet
if not errorlevel 1 (
    echo.
    echo Nothing new to upload - the logs are already saved in git.
    goto :pushanyway
)

REM --- Build a sortable timestamp for the message --------------
set "STAMP=%date% %time%"
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HH:mm:ss" 2^>nul') do set "STAMP=%%T"

echo Saving a new version ...
git commit -m "run logs %STAMP%"
if errorlevel 1 (
    echo [ERROR] git commit failed. See the message above.
    goto :end
)

:pushanyway
echo.
echo Uploading to the server ...
git push
if errorlevel 1 (
    echo.
    echo [ERROR] git push failed.
    echo.
    echo Common reasons:
    echo   - No internet connection
    echo   - You are not logged in to GitHub on this computer
    echo   - No remote set yet:  git remote add origin YOUR_REPO_URL
    echo   - First push needs:   git push -u origin main
    goto :end
)

echo.
echo ===========================================================
echo    Done. Logs uploaded.
echo ===========================================================

:end
echo.
pause
endlocal
