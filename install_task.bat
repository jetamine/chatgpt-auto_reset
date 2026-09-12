@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Create .venv and install requirements first; see README.md.
    exit /b 1
)
".venv\Scripts\python.exe" "app.py" --install-task
if errorlevel 1 (
    echo Failed to install scheduled task. See error above.
    exit /b 1
)
echo You can inspect the task named "ChatGPT Auto Hello" in Task Scheduler.
endlocal
