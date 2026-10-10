param(
    [Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role,
    [switch]$Purge,
    [switch]$RemoveExports
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'This uninstaller requires Windows.' }
$InstallRoot = Join-Path $env:LOCALAPPDATA "MCPTransfer\$Role"
$DataRoot = Join-Path $InstallRoot 'data'
$RuntimePython = Join-Path $InstallRoot 'runtime\Scripts\python.exe'
Set-Location -LiteralPath $env:TEMP
[System.Environment]::CurrentDirectory = $env:TEMP
$TaskName = "MCPTransfer-$Role"
. (Join-Path $PSScriptRoot 'windows-runtime.ps1')
Stop-McpAgent -Role $Role -DataRoot $DataRoot -IncludeBridges:$Purge
if ($RemoveExports -and $Role -eq 'client' -and (Test-Path $InstallRoot)) {
    if (-not (Test-Path $RuntimePython)) { throw 'Runtime missing. Clean exported application entries manually before purging.' }
    $OriginalPythonPath = $env:PYTHONPATH
    try {
        # Also clean older installations whose CLI does not yet have this command.
        $env:PYTHONPATH = Join-Path (Split-Path -Parent $PSScriptRoot) 'src'
        & $RuntimePython -m mcp_transfer --home $DataRoot clean-exports
        if ($LASTEXITCODE -ne 0) { throw 'Export cleanup failed. Files were retained; no profile data was deleted.' }
    } finally { $env:PYTHONPATH = $OriginalPythonPath }
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
if ($Purge) {
    Set-Location -LiteralPath $env:TEMP
    [System.Environment]::CurrentDirectory = $env:TEMP
    if (Test-Path $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
    Write-Host "Removed $Role runtime, credentials, connections and local data. Delete its device records in each cloud WebUI. Other roles and Python installations were preserved."
} else {
    Write-Host "Stopped and removed $TaskName. Runtime and all connection data preserved at $InstallRoot."
}
