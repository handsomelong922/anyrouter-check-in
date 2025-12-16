@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
REM AnyRouter 自动签到 - 一键设置定时任务
REM 请右键此文件，选择"以管理员身份运行"

echo ========================================
echo AnyRouter 自动签到 - 定时任务设置
echo ========================================
echo.

REM 检查是否以管理员身份运行
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 请右键此文件，选择"以管理员身份运行"！
    echo.
    pause
    exit /b 1
)

REM 设置变量
set "TASK_NAME=AnyRouter自动签到"
set "BAT_FILE=%~dp0run_checkin.bat"

REM 检查批处理文件
if not exist "%BAT_FILE%" (
    echo [错误] 未找到 run_checkin.bat 文件！
    pause
    exit /b 1
)

REM 检查任务是否已存在
schtasks /query /tn "%TASK_NAME%" >nul 2>&1
if %errorlevel% equ 0 (
    echo [信息] 任务已存在，删除旧任务...
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
)

echo [信息] 正在创建定时任务...
echo   任务名称: %TASK_NAME%
echo   执行间隔: 每 6 小时
echo   脚本路径: %BAT_FILE%
echo.

REM 创建定时任务
schtasks /create /tn "%TASK_NAME%" /tr "\"%BAT_FILE%\"" /sc daily /st 00:00 /ri 360 /du 9999:00 /rl HIGHEST /f

if !errorlevel! neq 0 (
    echo [错误] 创建定时任务失败！错误码: !errorlevel!
    echo.
    echo [提示] 可能的原因：
    echo   1. 未以管理员身份运行
    echo   2. 任务计划程序服务未启动
    pause
    exit /b 1
)

echo [成功] 定时任务创建成功！
echo.
echo ========================================
echo 任务设置详情：
echo ========================================
echo   任务名称: %TASK_NAME%
echo   执行间隔: 每 6 小时（00:00、06:00、12:00、18:00）
echo   权限级别: 最高
echo.

REM 查询下次运行时间
for /f "tokens=2 delims=:" %%a in ('schtasks /query /tn "%TASK_NAME%" /fo list ^| findstr /C:"下次运行时间"') do (
    echo   下次运行时间:%%a
)

echo.
echo [提示] 你可以在「任务计划程序」中查看任务
echo [提示] 快捷键：Win + R -^> 输入 taskschd.msc
echo.

REM 询问是否立即运行测试
set /p "RUN_NOW=是否立即运行一次测试？(Y/N): "
if /i "!RUN_NOW!"=="Y" (
    echo.
    echo ========================================
    echo 开始测试运行（实时显示输出）
    echo ========================================
    echo.
    call "%BAT_FILE%" manual
    echo.
    echo ========================================
    echo 测试完成！
    echo ========================================
    echo.
    echo [提示] 定时任务已设置，将在以下时间自动运行：
    echo   - 00:00（午夜）
    echo   - 06:00（清晨）
    echo   - 12:00（中午）
    echo   - 18:00（傍晚）
    echo.
)

echo.
echo ========================================
echo 设置完成！
echo ========================================
echo.
echo [提示] 查看任务运行状态：
echo   Win + R -^> 输入 taskschd.msc -^> 找到"AnyRouter自动签到"
echo.
pause
