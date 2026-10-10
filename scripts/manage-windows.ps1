param(
    [Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role,
    [ValidateSet('start','stop','restart','status')][string]$Action = 'status'
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This helper requires Windows.' }
$InstallRoot = Join-Path $env:LOCALAPPDATA "MCPTransfer\$Role"
$DataRoot = Join-Path $InstallRoot 'data'
$TaskName = "MCPTransfer-$Role"
Set-Location -LiteralPath $env:TEMP
[System.Environment]::CurrentDirectory = $env:TEMP
. (Join-Path $PSScriptRoot 'windows-runtime.ps1')
if ($Action -eq 'stop' -or $Action -eq 'restart') { Stop-McpAgent -Role $Role -DataRoot $DataRoot }
if ($Action -eq 'start' -or $Action -eq 'restart') { Start-ScheduledTask -TaskName $TaskName }
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
