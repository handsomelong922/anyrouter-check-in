# AnyRouter 自动签到 - Windows任务计划程序设置脚本
# 以管理员身份运行此脚本以自动创建定时任务

param(
    [string]$Interval = "6"  # 默认每6小时运行一次
)

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "AnyRouter 自动签到 - 定时任务设置" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 获取当前脚本所在目录
$ScriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatchFile = Join-Path $ScriptPath "run_checkin.bat"

# 检查批处理文件是否存在
if (-not (Test-Path $BatchFile)) {
    Write-Host "[错误] 未找到 run_checkin.bat 文件！" -ForegroundColor Red
    Write-Host "请确保此脚本与 run_checkin.bat 在同一目录下" -ForegroundColor Yellow
    pause
    exit 1
}

# 任务名称
$TaskName = "AnyRouter自动签到"

# 检查任务是否已存在
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($ExistingTask) {
    Write-Host "[警告] 任务 '$TaskName' 已存在！" -ForegroundColor Yellow
    $Response = Read-Host "是否要删除现有任务并重新创建？(Y/N)"
    if ($Response -eq "Y" -or $Response -eq "y") {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[信息] 已删除现有任务" -ForegroundColor Green
    } else {
        Write-Host "[信息] 保留现有任务，退出设置" -ForegroundColor Yellow
        pause
        exit 0
    }
}

Write-Host ""
Write-Host "[信息] 正在创建定时任务..." -ForegroundColor Cyan
Write-Host "  任务名称: $TaskName"
Write-Host "  执行间隔: 每 $Interval 小时"
Write-Host "  脚本路径: $BatchFile"
Write-Host ""

try {
    # 创建任务动作
    $Action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$BatchFile`""

    # 创建多个触发器（每6小时一次，从不同起始时间开始）
    $Triggers = @()
    $HoursInterval = [int]$Interval
    $TriggerCount = [Math]::Floor(24 / $HoursInterval)

    for ($i = 0; $i -lt $TriggerCount; $i++) {
        $StartHour = $i * $HoursInterval
        $StartTime = (Get-Date).Date.AddHours($StartHour)
        $Trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
        # 设置重复间隔
        $Trigger.Repetition = New-ScheduledTaskRepetition -Interval (New-TimeSpan -Hours $HoursInterval) -Duration ([TimeSpan]::MaxValue)
        $Triggers += $Trigger
    }

    # 创建任务设置
    $Settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RunOnlyIfNetworkAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    # 使用当前用户运行
    $Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U

    # 注册任务
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Triggers[0] `
        -Settings $Settings `
        -Principal $Principal `
        -Description "AnyRouter 自动签到脚本 - 每${Interval}小时运行一次" | Out-Null

    Write-Host "[成功] 定时任务创建成功！" -ForegroundColor Green
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "任务设置详情：" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  任务名称: $TaskName" -ForegroundColor White
    Write-Host "  执行间隔: 每 $Interval 小时" -ForegroundColor White
    Write-Host "  下次运行: $(Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo | Select-Object -ExpandProperty NextRunTime)" -ForegroundColor White
    Write-Host ""
    Write-Host "[提示] 你可以在「任务计划程序」中查看和管理此任务" -ForegroundColor Yellow
    Write-Host "[提示] 路径：控制面板 -> 管理工具 -> 任务计划程序" -ForegroundColor Yellow
    Write-Host ""

    # 询问是否立即运行一次
    $RunNow = Read-Host "是否立即运行一次测试？(Y/N)"
    if ($RunNow -eq "Y" -or $RunNow -eq "y") {
        Write-Host ""
        Write-Host "[信息] 正在运行签到脚本..." -ForegroundColor Cyan
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
        Write-Host "[信息] 任务已启动，请稍后查看任务历史记录" -ForegroundColor Green
    }

} catch {
    Write-Host "[错误] 创建定时任务失败: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host ""
    Write-Host "[提示] 请尝试以管理员身份运行此脚本" -ForegroundColor Yellow
    pause
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "设置完成！" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
pause
