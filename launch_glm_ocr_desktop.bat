@echo off
setlocal

set "PYTHON_EXE=G:\BaseWare\Anaconda\envs\glm-ocr\python.exe"
if not exist "%PYTHON_EXE%" (
    echo Python executable not found: %PYTHON_EXE%
    echo Please edit launch_glm_ocr_desktop.bat to point to your glm-ocr environment.
    pause
    exit /b 1
)

set "ROOT=%~dp0"
set "SERVER_LOG=%ROOT%glm_ocr_server.log"

echo Starting local GLM-OCR server...
start "GLM-OCR Local Server" /min cmd /c ""%PYTHON_EXE%" "%ROOT%glm_ocr_local_server.py" > "%SERVER_LOG%" 2>&1"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$url='http://127.0.0.1:5002/health';" ^
    "$deadline=(Get-Date).AddMinutes(3);" ^
    "while((Get-Date) -lt $deadline) { try { if((Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 $url).StatusCode -eq 200) { exit 0 } } catch { Start-Sleep -Seconds 2 } }" ^
    "exit 1"

if errorlevel 1 (
    echo Local GLM-OCR server did not become ready in time.
    echo See: "%SERVER_LOG%"
    pause
    exit /b 1
)

echo Starting web GUI...
start "GLM-OCR Web GUI" "%PYTHON_EXE%" "%ROOT%glm_ocr_web_gui.py"

endlocal
