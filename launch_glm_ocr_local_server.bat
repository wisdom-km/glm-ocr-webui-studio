@echo off
setlocal

set "PYTHON_EXE=G:\BaseWare\Anaconda\envs\glm-ocr\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Python executable not found: %PYTHON_EXE%
    pause
    exit /b 1
)

pushd "%~dp0"
"%PYTHON_EXE%" "%~dp0glm_ocr_local_server.py"
popd

endlocal
