@echo off
setlocal
chcp 65001 >nul

REM 双击手动运行（强制用 cmd /k 打开一个“不会自动关闭”的窗口，彻底解决“闪退”）
echo [Info] 正在打开命令窗口运行签到（窗口会保持打开）...
start "公益站-手动签到" cmd /k ""%~dp0run_checkin.bat" manual"
exit /b 0

