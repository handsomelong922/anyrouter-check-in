@echo off
setlocal enabledelayedexpansion

chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"

REM 切换到项目根目录（脚本所在目录的上级目录）
cd /d "%~dp0.."
set "PROJECT_ROOT=%CD%"

echo ========================================
echo 公益站 自动签到脚本启动
echo 时间: %date% %time%
echo 工作目录: %PROJECT_ROOT%
echo ========================================

REM 自动检测 uv 路径
set "UV_PATH="
for %%i in (uv.exe) do set "UV_PATH=%%~$PATH:i"
if not defined UV_PATH (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV_PATH=%USERPROFILE%\.local\bin\uv.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\uv\uv.exe" (
        set "UV_PATH=%LOCALAPPDATA%\Programs\uv\uv.exe"
    )
)

if not defined UV_PATH (
    echo [错误] 未找到 uv.exe
    echo [提示] 安装命令（PowerShell）:
    echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    if "%1"=="manual" pause
    exit /b 1
)
echo [信息] uv路径: %UV_PATH%

REM .env 非强制（本地可用 data/checkin.db；Actions 用 secrets）
if not exist "%PROJECT_ROOT%\.env" (
    echo [提示] 未找到 .env（如果你用数据库/环境变量配置账号，这是正常的）
)

echo.
if "%1"=="manual" (
    echo [信息] Running checkin script manual...
    "%UV_PATH%" run checkin.py --manual
    set "RUN_RESULT=!errorlevel!"

    echo.
    if !RUN_RESULT! equ 0 (
        echo [成功] 签到脚本执行完成
    ) else (
        echo [失败] 签到脚本执行失败，错误码: !RUN_RESULT!
    )

    echo ========================================
    echo 运行日志: %PROJECT_ROOT%\data\task_run.log
    echo ========================================
    pause
    exit /b !RUN_RESULT!
) else (
    echo [信息] Running checkin script task...
    "%UV_PATH%" run checkin.py
    exit /b %errorlevel%
)

