@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0Start_Local.bat" --check %*
set "check_result=%errorlevel%"
if "%check_result%"=="0" pause
exit /b %check_result%
