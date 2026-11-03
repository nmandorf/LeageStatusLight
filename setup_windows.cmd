@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto folder_error

echo Setting up Hubspace League lights...
echo.
if exist ".venv\Scripts\python.exe" goto check_environment

py -3.12 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_python312
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_launcher
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto create_python
goto python_missing

:create_python312
py -3.12 -m venv ".venv"
if errorlevel 1 goto environment_error
goto check_environment

:create_launcher
py -3 -m venv ".venv"
if errorlevel 1 goto environment_error
goto check_environment

:create_python
python -m venv ".venv"
if errorlevel 1 goto environment_error
goto check_environment

:check_environment
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto environment_error
if not exist "requirements.txt" goto files_missing
if not exist "configure_windows.py" goto files_missing

echo Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto dependencies_error

echo.
echo Configuring your lights and Hubspace login...
".venv\Scripts\python.exe" "configure_windows.py"
if errorlevel 1 goto configuration_error

echo.
echo Setup complete. Double-click start_league.cmd to start the watcher.
echo Use status_league.cmd to check it and stop_league.cmd to stop it.
echo You can also run: light.cmd league run --dry-run
echo.
pause
exit /b 0

:folder_error
echo Could not open the folder containing this script.
echo Extract the complete ZIP to a writable local folder and try again.
goto failed

:python_missing
echo Python 3.10 or newer was not found.
echo Install Python 3.12 from https://www.python.org/downloads/windows/
echo Include the Python launcher or select Add python.exe to PATH during installation.
echo Then run setup_windows.cmd again.
goto failed

:environment_error
echo Could not create or use the local Python environment.
echo Check that Python 3.10 or newer is installed and this folder is writable.
echo If .venv came from another computer, delete only the .venv folder and rerun setup.
goto failed

:files_missing
echo Required project files are missing.
echo Extract the entire ZIP before running setup_windows.cmd.
goto failed

:dependencies_error
echo Dependency installation failed. Review the error above.
echo Check your internet connection, then run setup_windows.cmd again.
goto failed

:configuration_error
echo Configuration or Hubspace login did not complete. Review the message above.
echo Run setup_windows.cmd again to retry.
goto failed

:failed
echo.
pause
exit /b 1
