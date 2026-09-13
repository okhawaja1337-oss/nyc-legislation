@echo off
cd /d "%~dp0"
python "%~dp0RUN_A_BRIEF.py"
if errorlevel 1 pause
