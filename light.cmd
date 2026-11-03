@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto folder_error
if not exist ".venv\Scripts\python.exe" goto setup_required
".venv\Scripts\python.exe" "light_cli.py" %*
exit /b %errorlevel%

:setup_required
echo Run setup_windows.cmd in this folder first to install and configure the lights.
exit /b 1

:folder_error
echo Could not open the project folder. Extract the complete ZIP to a local folder.
exit /b 1
