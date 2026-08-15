@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>nul && set "PY_CMD=py -3"
if not defined PY_CMD (
    where python >nul 2>nul && set "PY_CMD=python"
)

if not defined PY_CMD (
    echo [错误] 未找到 Python，请先安装 Python 3.9+，并勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

echo 正在启动键盘录制、回放与红点监控工具...
%PY_CMD% app.py
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
    echo.
    echo [提示] 程序异常退出，退出码：%EXIT_CODE%
    echo 你可以先执行：pip install -r requirements.txt
    echo.
    pause
)

exit /b %EXIT_CODE%
