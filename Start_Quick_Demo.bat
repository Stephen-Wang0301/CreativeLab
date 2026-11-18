@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0Start_Local.bat" --quick --local-only %*
set "check_result=%errorlevel%"
exit /b %check_result%
