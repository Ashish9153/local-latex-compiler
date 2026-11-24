@echo off
cd /d "%~dp0"
python latex_compiler.py %*
if errorlevel 1 py latex_compiler.py %*
pause
