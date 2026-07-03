@echo off
title Web Watcher
cd /d "%~dp0"
echo.
echo  Web Watcher
echo  -----------
echo  Starting services and opening dashboard...
echo.
python -m web_watcher.main
if %errorlevel% neq 0 (
    echo.
    echo  Web Watcher exited with an error. See output above.
    pause
)
