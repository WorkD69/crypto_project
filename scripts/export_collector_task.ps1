# Exports XML only. Does not register or enable a Windows scheduled task.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$taskRuntime = Join-Path $env:LOCALAPPDATA 'CryptoResearchPlatform\browser-collector'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$runnerPath = Join-Path $PSScriptRoot 'run_browser_collector.ps1'
$taskShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$taskAction = New-ScheduledTaskAction -Execute $taskShell `
    -Argument ('-NoProfile -NonInteractive -File "' + $runnerPath + '"') -WorkingDirectory $projectRoot
$taskTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(15)) `
    -RepetitionInterval (New-TimeSpan -Minutes 15)
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 12) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskDefinition = New-ScheduledTask -Action $taskAction -Trigger $taskTrigger `
    -Settings $taskSettings -Principal $taskPrincipal -Description 'Crypto Research Platform Stage 0 own-test collector'
$xmlPath = Join-Path $taskRuntime 'collector-task.xml'
Export-ScheduledTask -InputObject $taskDefinition | Set-Content -LiteralPath $xmlPath -Encoding Unicode
Write-Output ('XML exported only: ' + $xmlPath)
