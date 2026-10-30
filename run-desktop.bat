@echo off
setlocal
cd /d "%~dp0desktop"
".venv\Scripts\python.exe" -m photoarchive %*
endlocal
