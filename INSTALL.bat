@echo off
cd /d "%~dp0"
title HidenArm: setup
echo.
echo ===== HidenArm: setup and launch =====
echo.

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto no_python

echo [1/3] Installing the required libraries, internet is needed...
python -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto pip_failed

echo.
echo [2/3] Gemini API key
if defined GEMINI_API_KEY goto have_key
echo Get a free key at https://aistudio.google.com  (button "Get API key").
set /p "GEMINI_API_KEY=Paste the key here and press Enter: "
if not defined GEMINI_API_KEY goto no_key
setx GEMINI_API_KEY "%GEMINI_API_KEY%" >nul
echo The key was saved in Windows.
goto key_done
:have_key
echo A key is already set on this computer, skipping.
:key_done

echo.
echo [3/3] Starting the program...
wscript "%~dp0start.vbs"
echo.
echo DONE. The program runs in the background and has no window.
echo.
echo    Ctrl+Shift+X     solve the task, answer to the clipboard
echo    Ctrl+Shift+Z     solve and type the answer
echo    Ctrl+Shift+F10   settings and the full guide
echo    Ctrl+Shift+Q     quit the program
echo.
echo Next time just double-click start.vbs.
echo If it says the key is missing: sign out of Windows and sign in again.
echo.
pause
exit /b 0

:no_python
echo [!] Python 3.10 or newer was not found.
echo     Download it: https://www.python.org/downloads/
echo     IMPORTANT: on the first screen of the installer tick "Add python.exe to PATH".
echo     Then run this file again.
start "" https://www.python.org/downloads/
pause
exit /b 1

:pip_failed
echo [!] The libraries could not be installed. Check your internet connection and run this file again.
pause
exit /b 1

:no_key
echo [!] No key was entered. Run this file again and paste the key.
pause
exit /b 1
