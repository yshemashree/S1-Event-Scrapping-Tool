@echo off
rem Optional: run the scraper automatically every Friday at 07:00 with the
rem settings last saved in the app window. Remove with:
rem     schtasks /Delete /TN "S1 Event Scraper" /F
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Open "Run S1 Event Scraper.bat" once first, so the app is set up.
  pause
  exit /b 1
)
schtasks /Create /F /SC WEEKLY /D FRI /ST 07:00 /TN "S1 Event Scraper" /TR "\"%~dp0.venv\Scripts\python.exe\" \"%~dp0run_scraper.py\""
if errorlevel 1 (
  echo Could not create the scheduled task.
  pause
  exit /b 1
)
echo.
echo Done: the scraper will run every Friday at 07:00.
echo Keep the Excel file closed at that time, or results go to a copy next to it.
pause
