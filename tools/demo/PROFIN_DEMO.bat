@echo off
rem FAMILYFIN - DEMO (usuario DEMO / contrasena 123456). No toca los datos reales.
cd /d "%~dp0..\.."
python tools\demo\run_demo.py %*
if errorlevel 1 pause
