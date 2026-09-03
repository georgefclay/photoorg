@echo off
setlocal
cd /d "%~dp0"
"desktop\.venv\Scripts\python.exe" -m photoarchive %*
endlocal
