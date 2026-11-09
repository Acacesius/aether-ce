@echo off
setlocal
rem Start AETHER CE as a plain Python process on 127.0.0.1 (no venv, no pip). Needs Python 3.11+.
cd /d "%~dp0"
set "PYCMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if defined PYCMD goto found
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python"
if defined PYCMD goto found
python3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=python3"
if defined PYCMD goto found
echo Python 3.11 or newer is required but was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1
:found
set "PYTHONPATH=%~dp0"
%PYCMD% -m aether.studio serve --open %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
