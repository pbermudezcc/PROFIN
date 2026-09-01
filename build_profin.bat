@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python no encontrado en PATH.
    echo Instala Python 3.11+ y vuelve a ejecutar este archivo.
    pause
    exit /b 1
)

python -m pip install --upgrade pip >nul 2>&1
python -m pip install "pyinstaller>=6.11.0" --quiet

python tools\build_windows.py --channel beta
if errorlevel 1 (
    echo.
    echo ERROR: La compilacion de PROFIN fallo.
    pause
    exit /b %errorlevel%
)

echo.
echo Build de PROFIN completado.
echo El ejecutable se ha generado en la carpeta dist\
pause
