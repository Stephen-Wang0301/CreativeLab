@echo off
setlocal EnableExtensions DisableDelayedExpansion
call "%~dp0Start_Local.bat" --public %*
set "check_result=%errorlevel%"
exit /b %check_result%
