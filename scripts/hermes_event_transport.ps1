# One explicit own-text test using the installed Hermes CLI. No config writes.
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($request) -or $request.Length -gt 16000) { throw 'Invalid bounded request' }
$settingsPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'hermes-platform\scripts\local.settings.ps1'
if (-not (Test-Path -LiteralPath $settingsPath)) { throw 'Hermes SSH settings unavailable' }
. $settingsPath
$sshOptions = @('-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10')
if ($HermesIdentityFile) { $sshOptions += @('-i',$HermesIdentityFile) }
# Only the credential-free isolated Python toolset is available to this test;
# prompt forbids tool use and the caller rejects a response with any tool_use.
# --ignore-rules maps to skip_memory and skip_context_files in installed v0.21.6.
$remoteCommand = 'docker exec -i -u hermes -w /opt/data hermes-agent /opt/hermes/.venv/bin/hermes chat --query-file - --oneshot -Q --ignore-rules --source tool --toolsets python --max-turns 1 --run-budget 60 --reasoning low -m gpt-6-luna --format stream-json'
$request | & ssh @sshOptions $HermesServer $remoteCommand
exit $LASTEXITCODE
