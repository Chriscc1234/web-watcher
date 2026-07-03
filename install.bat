@echo off
title Web Watcher Installer
setlocal EnableExtensions

echo.
echo  Web Watcher Installer
echo  =====================
echo.

:: -----------------------------------------------------------------------
:: Check for Python 3.10+
:: -----------------------------------------------------------------------

python --version >nul 2>&1
if errorlevel 1 goto :no_python

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto :old_python

goto :run_installer

:: -----------------------------------------------------------------------
:: Python not found — try winget
:: -----------------------------------------------------------------------

:no_python
echo  Python not found.
echo  Attempting to install Python 3.12 via winget ...
echo.
winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :python_fail

:: Refresh the PATH so the newly installed python is visible
:: (winget installs to %LOCALAPPDATA%\Programs\Python\Python312\ by default)
set "NEW_PATH=%LOCALAPPDATA%\Programs\Python\Python312"
if exist "%NEW_PATH%\python.exe" (
    set "PATH=%NEW_PATH%;%NEW_PATH%\Scripts;%PATH%"
)

python --version >nul 2>&1
if errorlevel 1 goto :python_fail
goto :run_installer

:old_python
echo  Python 3.10 or newer is required.
echo  Attempting to upgrade via winget ...
echo.
winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :python_fail
:: Let the user restart with the new version
echo.
echo  Python 3.12 installed.  Please close this window and re-run install.bat.
pause
exit /b 0

:python_fail
echo.
echo  ERROR: Could not install Python automatically.
echo.
echo  Please install Python 3.10+ from https://www.python.org/downloads/
echo  Make sure to tick "Add Python to PATH" during installation.
echo  Then re-run this installer.
echo.
pause
exit /b 1

:: -----------------------------------------------------------------------
:: Run the Python installer script
:: -----------------------------------------------------------------------

:run_installer
echo  Using Python:
python --version
echo.

python "%~dp0install.py" %*
if errorlevel 1 (
    echo.
    echo  Installation failed.  See messages above.
    pause
    exit /b 1
)

pause
exit /b 0
