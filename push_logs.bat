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

REM  Screenshots and DOM dumps are ~20 MB per run and the upload times out
REM  on a slow link (HTTP 408), taking the text transcript down with it. The
REM  transcript is what gets read nearly every time, so that goes by default
REM  and the heavy artifacts only when asked for:
REM
REM      push_logs.bat          the run transcripts and bot.log
REM      push_logs.bat all      those plus screenshots and page dumps
REM
REM  Rejected captcha grids ride along regardless: they are small and they
REM  are the only record of what the solver was actually shown.
if /i "%~1"=="all" (
    echo Adding log files ^(including screenshots and page dumps^) ...
    git add logs/
    call :weigh
) else (
    echo Adding log files ^(text only - use "push_logs.bat all" for pictures^) ...
    REM  One pathspec at a time. git add is ATOMIC across pathspecs: if any
    REM  one of them matches nothing it fails with "did not match any files"
    REM  and stages NOTHING, so a machine with no bot.log yet would push no
    REM  transcripts either. Each is allowed to match nothing on its own.
    git add "logs/*.txt" 2>nul
    git add "logs/*.log" 2>nul
    git add "logs/captcha_samples/*_REJECTED_*.png" 2>nul
    REM  Record files the bot has PRUNED. Log retention deletes screenshots
    REM  and page dumps that are already tracked, and leaving those deletions
    REM  unstaged keeps the working tree permanently dirty. A deletion costs
    REM  nothing to upload - it is metadata, not content.
    git add -u logs/
)
REM  errorlevel is deliberately NOT checked here: the last git add above may
REM  legitimately match nothing. Whether anything is staged is decided next.

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
REM  A slow Egyptian link plus a big pack is how the upload dies with
REM  "RPC failed; HTTP 408". A bigger buffer and no low-speed cutoff let it
REM  take as long as it needs. Set on THIS repository only.
git config http.postBuffer 524288000
git config http.lowSpeedLimit 0
git config http.lowSpeedTime 999999

REM  The other machine pushes here too, so the branch is usually behind by
REM  the time this runs. Rebase first; without it every push is rejected as
REM  non-fast-forward and the logs never arrive.
REM  --autostash because the bot DELETES tracked files as it runs: log
REM  pruning removes screenshots and page dumps older than
REM  logging.retention_days, and git sees those as unstaged deletions. A plain
REM  rebase refuses to start with "cannot pull with rebase: You have unstaged
REM  changes", and the run logs never leave the machine. The deletions are set
REM  aside, the rebase runs, and they are put straight back.
echo Checking for other changes first ...
git pull --rebase --autostash
if errorlevel 1 (
    echo.
    echo [ERROR] could not merge the remote changes. Resolve them, then run
    echo         push_logs.bat again.
    goto :end
)

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
    echo.
    echo If the message above says "HTTP 408" or the upload is tens of MB,
    echo an OLDER commit is carrying screenshots and has to go up with
    echo everything after it. These lines unpick the waiting commits and put
    echo this computer back on the latest code. Nothing in logs is deleted -
    echo the pictures stay on disk, they just stop being queued for upload:
    echo.
    echo     git fetch origin
    echo     git reset --mixed origin/master
    echo     git checkout -- .
    echo     push_logs.bat
    echo.
    goto :end
)

echo.
echo ===========================================================
echo    Done. Logs uploaded.
echo ===========================================================
goto :end

REM --- Weigh the pictures before committing to them ------------
REM  A pack much over 12 MB dies with "RPC failed; HTTP 408" on this link.
REM  That failure is worse than it looks: the commit is already made, so it
REM  sits locally and blocks every later text-only push too, and unpicking it
REM  needs git commands the client should never have to type. Cheaper to weigh
REM  the staged files first and put the heavy ones back if they cannot fly.
:weigh
set "STAGEDMB=0"
for /f %%S in ('powershell -NoProfile -Command "$t=0; git diff --cached --name-only | ForEach-Object { if (Test-Path -LiteralPath $_) { $t += (Get-Item -LiteralPath $_).Length } }; [int]($t/1MB)" 2^>nul') do set "STAGEDMB=%%S"
echo   Pictures and page dumps staged: about %STAGEDMB% MB
if %STAGEDMB% LEQ 12 goto :eof
echo.
echo   [SKIPPED] That is too big for this connection - an upload this size
echo             fails part-way through and then blocks the next one. Sending
echo             the text logs only; the pictures stay on this computer.
echo             Ask for a specific screenshot if one is needed.
echo.
git reset -q
git add "logs/*.txt" 2>nul
git add "logs/*.log" 2>nul
git add "logs/captcha_samples/*_REJECTED_*.png" 2>nul
git add -u logs/
goto :eof

:end
echo.
pause
endlocal
