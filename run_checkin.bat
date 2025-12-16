@echo off
chcp 65001 >nul
REM AnyRouter 自动签到 - Windows定时任务启动脚本
REM 此脚本用于Windows任务计划程序调用

echo ========================================
echo AnyRouter 自动签到脚本启动
echo 时间: %date% %time%
echo ========================================

REM 切换到脚本所在目录
cd /d "%~dp0"

REM 检查uv是否安装
where uv >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未找到uv命令，请先安装uv！
    echo 安装方法: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
    pause
    exit /b 1
)

REM 运行签到脚本
echo.
echo [信息] 正在运行签到脚本...
uv run checkin.py

REM 记录运行状态
if %errorlevel% equ 0 (
    echo.
    echo [成功] 签到脚本执行完成
) else (
    echo.
    echo [失败] 签到脚本执行失败，错误码: %errorlevel%
)

echo ========================================
echo 执行结束: %date% %time%
echo ========================================

REM 如果手动运行，暂停等待查看结果
if "%1"=="manual" pause
