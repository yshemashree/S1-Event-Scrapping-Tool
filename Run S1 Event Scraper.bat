@echo off
rem Double-click to open the S1 Event Scraper window.
rem The first run creates a private Python environment in the .venv folder.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto run

echo Setting up S1 Event Scraper for the first time (needs internet, about a minute)...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY goto nopython
%PY% -m venv .venv || goto failed
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto failed

:run
start "" ".venv\Scripts\pythonw.exe" "%~dp0run_scraper.py" --gui
exit /b 0

:nopython
echo.
echo Python 3 is not installed on this computer.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then double-click this file again.
pause
exit /b 1

:failed
echo.
echo Setup did not finish - check the internet connection and double-click this file again.
rmdir /s /q .venv 2>nul
pause
exit /b 1
