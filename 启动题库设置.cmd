@echo off
setlocal
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\wizard.ps1"
if errorlevel 1 pause
