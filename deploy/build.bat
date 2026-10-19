@echo off
setlocal
REM Package code.zip (delegates to build.ps1, which writes forward-slash zip paths)
set "SCRIPT_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%build.ps1"
if errorlevel 1 (
    echo Build failed. See error above.
    exit /b 1
)
endlocal