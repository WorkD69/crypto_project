param(
    [ValidatePattern('^[a-z0-9_-]+$')][string]$Source = 'saved',
    [ValidateRange(60, 900)][int]$TimeoutSeconds = 600,
    [switch]$Headed
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$collectorPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$collectorRuntime = Join-Path $env:LOCALAPPDATA 'CryptoResearchPlatform\browser-collector'
$logDirectory = Join-Path $collectorRuntime 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$runStamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
$outputPath = Join-Path $logDirectory ($runStamp + '.json')
$errorPath = Join-Path $logDirectory ($runStamp + '.stderr.txt')
$collectorArguments = @('-u', '-m', 'scripts.telegram_browser_collector', 'collect', '--source', $Source)
if (-not $Headed) { $collectorArguments += '--headless' }
$worker = Start-Process -FilePath $collectorPython -ArgumentList $collectorArguments `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $outputPath -RedirectStandardError $errorPath
$null = $worker.Handle
if (-not $worker.WaitForExit($TimeoutSeconds * 1000)) {
    # Only the process tree created by this invocation is terminated.
    & taskkill.exe /PID $worker.Id /T /F | Out-Null
    Write-Output ('{"state":"ERROR","code":"RUN_TIMEOUT","worker_pid":' + $worker.Id + '}')
    exit 3
}
$worker.WaitForExit()
if (Test-Path -LiteralPath $outputPath) { Get-Content -LiteralPath $outputPath }
exit $worker.ExitCode
