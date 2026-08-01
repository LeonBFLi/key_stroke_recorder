@echo off
setlocal
cd /d "%~dp0"
py -m pip install -r requirements.txt || goto :error
py -m PyInstaller --noconfirm --clean --onefile --windowed --name KeyStrokeRecorder app.py || goto :error
echo.
echo Build complete: dist\KeyStrokeRecorder.exe
pause
exit /b 0

:error
echo.
echo Build failed. See the error above.
pause
exit /b 1
