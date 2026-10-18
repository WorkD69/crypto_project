# Session-scoped tool selection only. No Hermes configuration or credentials writes.
param([string]$SessionId = '')
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($request) -or $request.Length -gt 16000) { throw 'Invalid bounded request' }
if ($SessionId -and $SessionId -notmatch '^20[0-9]{6}_[0-9]{6}_[a-f0-9]{6}$') { throw 'Invalid session ID' }
$settingsPath = Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'hermes-platform\scripts\local.settings.ps1'
. $settingsPath
$sshOptions = @('-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10')
if ($HermesIdentityFile) { $sshOptions += @('-i',$HermesIdentityFile) }
# Installed model_tools.get_tool_definitions(['no_mcp']) was verified to return []
# without a model request. Existing CLI handles OAuth; no OAuth operations here.
$remoteCommand = 'docker exec -i -u hermes -w /opt/data hermes-agent /opt/hermes/.venv/bin/hermes chat --query-file - --oneshot -Q --ignore-rules --source tool --toolsets no_mcp --max-turns 1 --run-budget 60 --reasoning low -m gpt-6-luna --format stream-json'
if ($SessionId) { $remoteCommand += ' --no-restore-cwd --resume ' + $SessionId }
$request | & ssh @sshOptions $HermesServer $remoteCommand
exit $LASTEXITCODE
