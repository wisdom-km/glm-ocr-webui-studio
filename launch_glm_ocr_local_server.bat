@echo off
setlocal

set "ROOT=%~dp0"
set "LOG_DIR=%ROOT%logs\runtime"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "SERVER_LOG=%LOG_DIR%\glm_ocr_local_server.log"

call :resolve_python
if errorlevel 1 exit /b 1

pushd "%ROOT%"
%PYTHON_CMD% "%ROOT%glm_ocr_local_server.py" >> "%SERVER_LOG%" 2>&1
popd

endlocal
exit /b 0

:resolve_python
where conda >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=conda run -n glm-ocr python"
    exit /b 0
)

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    exit /b 0
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)

echo Python not found. Install Python or Conda and make sure it is on PATH.
pause
exit /b 1
