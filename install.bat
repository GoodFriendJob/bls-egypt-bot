@echo off
REM ===========================================================
REM  BLS Appointment Bot - Installation
REM  Run this ONE TIME before using the bot.
REM ===========================================================
setlocal
cd /d "%~dp0"

echo ===========================================================
echo    BLS Appointment Bot - Installation
echo ===========================================================
echo.

REM --- Step 0: is Python on this computer? -------------------
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python was not found on this computer.
    echo.
    echo Please do this:
    echo   1. Go to  https://www.python.org/downloads/
    echo   2. Download Python 3.11 or newer
    echo   3. IMPORTANT: tick the box "Add Python to PATH" while installing
    echo   4. Restart this window and run install.bat again
    goto :end
)
for /f "tokens=*" %%v in ('python --version') do echo Python found: %%v
echo.

REM --- Step 1: virtual environment ---------------------------
if exist ".venv\Scripts\python.exe" (
    echo Virtual environment already exists. Using it.
) else (
    echo Creating the virtual environment ^(folder .venv^) ...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo [ERROR] Could not create the virtual environment.
        echo Check that you have permission to write in this folder.
        goto :end
    )
    echo Virtual environment created.
)
echo.

REM --- Step 2: pip -------------------------------------------
echo [1 of 3] Updating pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
    echo.
    echo [ERROR] Could not update pip. Check your internet connection.
    goto :end
)
echo.

REM --- Step 3: packages --------------------------------------
echo [2 of 3] Installing the Python packages ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Could not install the packages.
    echo Check your internet connection and run install.bat again.
    goto :end
)
echo.

REM --- Step 4: browser ---------------------------------------
echo [3 of 3] Downloading the Chromium browser ^(about 140 MB^) ...
echo This part is slow. Please wait, do not close this window.
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Could not download the browser.
    echo Check your internet connection and run install.bat again.
    goto :end
)
echo.

REM --- Step 5: config file -----------------------------------
REM  Never overwrite an existing config.yaml - it holds real passwords.
if exist "config.yaml" (
    echo config.yaml already exists. Keeping your file as it is.
) else (
    echo Creating config.yaml from the template ...
    copy /y "config.example.yaml" "config.yaml" >nul
    if errorlevel 1 (
        echo [WARNING] Could not create config.yaml.
        echo Please copy config.example.yaml to config.yaml by hand.
    ) else (
        echo config.yaml created.
        echo IMPORTANT: open config.yaml and put your email, password
        echo and Telegram details inside before starting the bot.
    )
)

echo.
echo ===========================================================
echo    Installation complete!
echo ===========================================================
echo.
echo What to do next:
echo   1. Open config.yaml and fill in your details
echo   2. Put your documents in the docs folder:
echo        docs\passport.pdf
echo        docs\photo.jpg
echo   3. Run  start.bat  to start the bot
echo.
echo NOTE: the BLS website only answers to internet connections
echo       inside Egypt. If you see "403 Forbidden", connect from
echo       Egypt ^(or use an Egyptian VPS / VPN^) and try again.

:end
echo.
pause
endlocal
