@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   HaluLens - 视觉大模型幻觉检测演示
echo ------------------------------------------------------------
echo   服务启动后会自动打开浏览器
echo   地址: http://127.0.0.1:8000
echo   关闭这个窗口就会停止服务
echo ============================================================
echo.

if not exist ".env" (
    echo [错误] 找不到 .env 文件。
    echo        请先把 .env.example 复制为 .env 并填入 VLM_API_KEY。
    echo.
    pause
    exit /b 1
)

rem 等 3 秒再打开浏览器，给服务一点启动时间
start "" cmd /c "timeout /t 3 >nul & start http://127.0.0.1:8000"

python code\server.py

echo.
echo 服务已停止。
pause
