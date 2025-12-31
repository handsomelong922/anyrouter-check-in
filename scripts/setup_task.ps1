param(
	[string]$TaskName = '公益站自动签到'
)

$ErrorActionPreference = 'Stop'

function Assert-Admin {
	$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
	$principal = [Security.Principal.WindowsPrincipal]$identity
	if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
		throw '请以管理员身份运行（右键 setup_task.bat -> 以管理员身份运行）。'
	}
}

Assert-Admin

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$batPath = Join-Path $projectRoot 'scripts\run_checkin.bat'

if (-not (Test-Path -LiteralPath $batPath)) {
	throw "未找到 run_checkin.bat：$batPath"
}

# 任务动作：用 cmd.exe 执行 bat（任务计划更稳定）
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ("/c `"$batPath`"")

# 触发器：每天固定 4 次（00/06/12/18）
$triggers = @(
	(New-ScheduledTaskTrigger -Daily -At 00:00),
	(New-ScheduledTaskTrigger -Daily -At 06:00),
	(New-ScheduledTaskTrigger -Daily -At 12:00),
	(New-ScheduledTaskTrigger -Daily -At 18:00)
)

# 设置：错过时间就补跑；多实例忽略新触发；允许电池供电
$settings = New-ScheduledTaskSettingsSet `
	-StartWhenAvailable `
	-AllowStartIfOnBatteries `
	-DontStopIfGoingOnBatteries `
	-MultipleInstances IgnoreNew `
	-ExecutionTimeLimit (New-TimeSpan -Hours 2)

# 以当前用户“仅登录时运行”的方式创建（不需要保存密码）
# 兼容不同 Windows/PowerShell 版本的 LogonType 枚举：
# - 某些系统支持 InteractiveToken（较新）
# - 某些系统只支持 Interactive（较旧/更常见）
$userId = if ($env:USERDOMAIN) { "$($env:USERDOMAIN)\$($env:USERNAME)" } else { $env:USERNAME }

$logonType = $null
try {
	$enumType = [Microsoft.PowerShell.Cmdletization.GeneratedTypes.ScheduledTask.LogonTypeEnum]
	$names = [enum]::GetNames($enumType)
	if ($names -contains 'InteractiveToken') {
		$logonType = 'InteractiveToken'
	} elseif ($names -contains 'Interactive') {
		$logonType = 'Interactive'
	} elseif ($names -contains 'InteractiveOrPassword') {
		$logonType = 'InteractiveOrPassword'
	}
} catch {
	# ignore and let it fall back
}

$principal = if ($logonType) {
	New-ScheduledTaskPrincipal -UserId $userId -LogonType $logonType -RunLevel Highest
} else {
	New-ScheduledTaskPrincipal -UserId $userId -RunLevel Highest
}

$task = New-ScheduledTask -Action $action -Trigger $triggers -Settings $settings -Principal $principal

try {
	Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false | Out-Null
} catch {
	# ignore if not exists
}

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null

Write-Host '========================================'
Write-Host '✅ 定时任务创建/更新成功'
Write-Host "任务名称: $TaskName"
Write-Host "脚本路径: $batPath"
Write-Host '触发时间: 00:00 / 06:00 / 12:00 / 18:00（每天）'
Write-Host '========================================'

try {
	$info = Get-ScheduledTaskInfo -TaskName $TaskName
	Write-Host "上次运行时间: $($info.LastRunTime)"
	Write-Host "上次运行结果: $($info.LastTaskResult)"
	Write-Host "下次运行时间: $($info.NextRunTime)"
} catch {
	# ignore
}

