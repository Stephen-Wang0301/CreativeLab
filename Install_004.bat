@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0Start_Local.bat" --install-004 %*
set "check_result=%errorlevel%"
exit /b %check_result%
