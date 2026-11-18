@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
set "study_root=%~dp0"
set "study_diag_dir=%LOCALAPPDATA%\CreativeStudy"
if not defined LOCALAPPDATA set "study_diag_dir=%TEMP%\CreativeStudy"
if not exist "%study_diag_dir%\." mkdir "%study_diag_dir%" 2>nul
set "study_diag=%study_diag_dir%\windows-launch.log"
>"%study_diag%" echo [%DATE% %TIME%] CreativeStudy launcher
if not exist "%study_root%run.py" goto incomplete

rem An already activated Conda or virtual environment supplies DLL paths.
if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" (
  set "study_python=%CONDA_PREFIX%\python.exe"
  call :probe
  if not errorlevel 1 goto run_python
)
if defined VIRTUAL_ENV if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
  set "study_python=%VIRTUAL_ENV%\Scripts\python.exe"
  call :probe
  if not errorlevel 1 goto run_python
)

rem An inactive Conda environment is activated by Conda itself.
if defined CONDA_EXE if exist "%CONDA_EXE%" (
  set "study_conda=%CONDA_EXE%"
  call :probe_conda
  if not errorlevel 1 goto run_conda
)
if exist "D:\Development\Runtimes\Anaconda3\Scripts\conda.exe" (
  set "study_conda=D:\Development\Runtimes\Anaconda3\Scripts\conda.exe"
  call :probe_conda
  if not errorlevel 1 goto run_conda
)
for /f "delims=" %%I in ('%SystemRoot%\System32\where.exe conda.exe 2^>nul') do (
  set "study_conda=%%I"
  call :probe_conda
  if not errorlevel 1 goto run_conda
)

set "study_python=%study_root%runtime\win-amd64\python.exe"
if exist "%study_python%" (
  call :probe
  if not errorlevel 1 goto run_python
)
for /f "delims=" %%I in ('%SystemRoot%\System32\where.exe python.exe 2^>nul') do (
  set "study_python=%%I"
  call :probe
  if not errorlevel 1 goto run_python
)
goto missing

:probe
>>"%study_diag%" echo Checking "%study_python%"
"%study_python%" -c "import sys,sqlite3,ssl; print(sys.executable); print(sys.version); sys.exit(sys.version_info[:2] < (3,11))" >>"%study_diag%" 2>&1
exit /b %errorlevel%

:probe_conda
>>"%study_diag%" echo Checking skillcourse with "%study_conda%"
"%study_conda%" run --no-capture-output -n skillcourse -- python -c "import sys,sqlite3,ssl; print(sys.executable); print(sys.version); sys.exit(sys.version_info[:2] < (3,11))" >>"%study_diag%" 2>&1
exit /b %errorlevel%

:run_python
>>"%study_diag%" echo Starting with "%study_python%"
"%study_python%" -u "%study_root%run.py" %*
set "study_status=%errorlevel%"
goto finish

:run_conda
>>"%study_diag%" echo Starting with Conda skillcourse
"%study_conda%" run --no-capture-output -n skillcourse -- python -u "%study_root%run.py" %*
set "study_status=%errorlevel%"
goto finish

:incomplete
echo [CreativeStudy] Extract the entire ZIP before starting.
set "study_status=2"
goto finish

:missing
echo [CreativeStudy] No usable Python interpreter was found.
echo [CreativeStudy] Open Anaconda Prompt, run conda activate skillcourse, then run Install_004.bat.
set "study_status=2"

:finish
>>"%study_diag%" echo Exit code: %study_status%
if "%study_status%"=="0" exit /b 0
echo.
echo Python checks: "%study_diag%"
echo Service log: "%study_root%logs\startup.log"
pause
exit /b %study_status%
