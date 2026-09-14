@echo off
cd /d "%~dp0"
python "%~dp0SHARE_WITH_STAFF.py"
if errorlevel 1 pause
