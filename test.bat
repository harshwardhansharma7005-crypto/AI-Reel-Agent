@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_tests.ps1" %*
pause
