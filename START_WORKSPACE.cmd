@echo off
title D49 Workspace
cd /d "%~dp0"
if exist "runtime\python.exe" (
  "runtime\python.exe" START_WORKSPACE.py %*
  if errorlevel 1 pause
  exit /b
)
where py >nul 2>nul
if %errorlevel% equ 0 (
  py -3 START_WORKSPACE.py %*
) else (
  where python >nul 2>nul
  if %errorlevel% equ 0 (
    python START_WORKSPACE.py %*
  ) else (
    echo.
    echo   Python was not found on this computer.
    echo   Install Python 3.11 from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
  )
)
if errorlevel 1 pause
