@echo off
setlocal
chcp 65001 > nul
cd /d "%~dp0"

where py > nul 2>&1
if errorlevel 1 (
    echo Не найден Python Launcher.
    echo Установите Python 3.11 или 3.12 x64.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    py -3 -m venv .venv
    if errorlevel 1 goto error
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto error

".venv\Scripts\python.exe" -m pip install PySide6 pyserial pyinstaller
if errorlevel 1 goto error

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean ezSTM.spec
if errorlevel 1 goto error

echo.
echo Сборка завершена:
echo %CD%\dist\ezSTM.exe
echo.
pause
exit /b 0

:error
echo.
echo Ошибка сборки. Смотрите сообщения выше.
pause
exit /b 1
