# Approved experiment-only runtime overrides. Existing config and services are untouched.
param([switch]$Preflight)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd()
if (-not $Preflight -and ([string]::IsNullOrWhiteSpace($request) -or $request.Length -gt 28000)) { throw 'Invalid bounded request' }
$settingsPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'hermes-platform\scripts\local.settings.ps1'
. $settingsPath
$sshOptions = @('-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10')
if ($HermesIdentityFile) { $sshOptions += @('-i',$HermesIdentityFile) }
$loader = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'hermes_bounded_runtime.py')))
# Source is sent as base64 code, while stdin carries only the single bounded prompt.
$remoteCommand = 'docker exec -i -u hermes -w /opt/hermes hermes-agent /opt/hermes/.venv/bin/python -c "import base64;exec(compile(base64.b64decode(''' + $loader + '''),''crp_bounded_runtime'',''exec''))" chat --query-file - --oneshot -Q --ignore-rules --source tool --toolsets no_mcp --max-turns 1 --run-budget 75 --reasoning low -m gpt-6-luna --format stream-json'
if ($Preflight) { $remoteCommand = $remoteCommand.Substring(0,$remoteCommand.IndexOf(' chat --query-file')) + ' --crp-preflight' }
# Windows PowerShell 5's native argv bridge otherwise strips the Python -c quotes.
$request | & ssh @sshOptions $HermesServer ($remoteCommand.Replace('"','\"'))
exit $LASTEXITCODE
