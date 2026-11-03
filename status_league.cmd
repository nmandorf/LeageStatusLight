@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto folder_error
call light.cmd league status
set "league_exit=%errorlevel%"
goto finished

:folder_error
echo Could not open the project folder. Extract the complete ZIP to a local folder.
set "league_exit=1"

:finished
echo.
pause
exit /b %league_exit%
