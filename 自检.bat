@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   HaluLens - 离线自检（不联网、不消耗 API 额度）
echo ------------------------------------------------------------
echo   这一步验证代码本身是好的，不涉及任何模型调用。
echo   预期输出最后一行： 结果：全部通过
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 goto NOPYTHON

python --version

rem 仓库里路径是 code\selftest.py；交付包里源码在 源码\code\ 下，两种都认
set SELFTEST=code\selftest.py
if not exist "%SELFTEST%" set SELFTEST=源码\code\selftest.py
if not exist "%SELFTEST%" goto NOFILE

echo.
python "%SELFTEST%"
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" goto FAILED
echo [完成] 自检通过，可以双击 启动演示.bat 打开网页界面。
echo.
pause
exit /b 0

:NOPYTHON
echo [错误] 找不到 python 命令。
echo        请先安装 Python 3.11+，安装时勾选 Add Python to PATH。
echo.
pause
exit /b 1

:NOFILE
echo [错误] 找不到 selftest.py，请在项目根目录运行本脚本。
echo        仓库根目录应有 code\selftest.py，交付包根目录应有 源码\code\selftest.py
echo.
pause
exit /b 1

:FAILED
echo [失败] 自检未通过，上面列出了具体失败项。
echo.
pause
exit /b %RC%
