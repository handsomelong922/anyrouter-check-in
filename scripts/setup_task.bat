@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

echo ========================================
echo 公益站 自动签到 - 定时任务设置
echo ========================================
echo.

REM 检查是否以管理员身份运行
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键此文件，选择“以管理员身份运行”！
    echo.
    pause
    exit /b 1
)

REM 默认任务名：优先沿用已存在的“自动签到”，避免你之前更名后对不上
set "TASK_NAME="
REM 支持自定义任务名：setup_task.bat "我的任务名"
if not "%~1"=="" (
    set "TASK_NAME=%~1"
) else (
    schtasks /query /tn "自动签到" >nul 2>&1
    if !errorlevel! equ 0 (
        set "TASK_NAME=自动签到"
    ) else (
        set "TASK_NAME=公益站自动签到"
    )
)

set "BAT_FILE=%~dp0run_checkin.bat"
if not exist "%BAT_FILE%" (
    echo [错误] 未找到 run_checkin.bat：%BAT_FILE%
    pause
    exit /b 1
)

echo [信息] 正在创建/更新定时任务...
echo   任务名称: %TASK_NAME%
echo   触发时间: 00:00、06:00、12:00、18:00（每天）
echo   脚本路径: %BAT_FILE%
echo.

REM 使用 PowerShell 创建任务（多触发器 + 错过自动补跑，更稳定）
set "PWSH_EXE=powershell.exe"
if exist "%ProgramFiles%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles%\PowerShell\7\pwsh.exe"
if exist "%ProgramFiles(x86)%\PowerShell\7\pwsh.exe" set "PWSH_EXE=%ProgramFiles(x86)%\PowerShell\7\pwsh.exe"

"%PWSH_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_task.ps1" -TaskName "%TASK_NAME%"
if !errorlevel! neq 0 (
    echo [错误] 创建定时任务失败！错误码: !errorlevel!
    echo.
    pause
    exit /b 1
)

echo.
echo [成功] 定时任务创建/更新成功！
echo [提示] 你可以在「任务计划程序」中查看：Win + R -> taskschd.msc
echo.
echo [提示] 立即手动测试（不会闪退）：scripts\run_checkin_manual.bat
echo.
pause

