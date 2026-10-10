param(
    [Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role,
    [int]$Port = 0
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows.' }
$SourceRoot = Split-Path -Parent $PSScriptRoot
$InstallRoot = Join-Path $env:LOCALAPPDATA "MCPTransfer\$Role"
$RuntimeRoot = Join-Path $InstallRoot 'runtime'
$DataRoot = Join-Path $InstallRoot 'data'
$Python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $Python)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw 'Install Python 3.12 from python.org, or enable Windows App Installer (winget), then retry.' }
    & winget install --id Python.Python.3.12 --exact --scope user --accept-package-agreements --accept-source-agreements --silent
    if ($LASTEXITCODE -ne 0 -and -not (Test-Path $Python)) { throw 'Python installation failed.' }
}
if (-not (Test-Path $Python)) { throw 'Python 3.12 was not found in its per-user installation directory.' }
New-Item -ItemType Directory -Force -Path $InstallRoot, $DataRoot | Out-Null
$Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls $InstallRoot /inheritance:r /grant:r "${Identity}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' /Q | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Cannot restrict installation directory permissions.' }
foreach ($Item in Get-ChildItem -Force -LiteralPath $InstallRoot) {
    & icacls $Item.FullName /reset /T /Q | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot propagate private installation permissions.' }
}
$TaskName = "MCPTransfer-$Role"
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask) { Stop-ScheduledTask -TaskName $TaskName; Start-Sleep -Seconds 2 }
if (-not (Test-Path (Join-Path $RuntimeRoot 'Scripts\python.exe'))) {
    & $Python -m venv $RuntimeRoot
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create the Python environment.' }
}
$RuntimePython = Join-Path $RuntimeRoot 'Scripts\python.exe'
& $RuntimePython -m pip install --disable-pip-version-check -c (Join-Path $SourceRoot 'requirements.lock') $SourceRoot
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Previous data was preserved.' }
if ($Role -eq 'host') {
    & $RuntimePython -m mcp_transfer --home $DataRoot install-frp --binary frpc
    if ($LASTEXITCODE -ne 0) { throw 'frp download or checksum verification failed.' }
}
if ($Port -eq 0) { if ($Role -eq 'host') { $Port = 17810 } else { $Port = 17820 } }
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Port must be between 1024 and 65535.' }
$PythonW = Join-Path $RuntimeRoot 'Scripts\pythonw.exe'
$Arguments = "-m mcp_transfer --home `"$DataRoot`" $Role --port $Port"
$TaskAction = New-ScheduledTaskAction -Execute $PythonW -Argument $Arguments -WorkingDirectory $InstallRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $Identity
$Principal = New-ScheduledTaskPrincipal -UserId $Identity -LogonType Interactive -RunLevel Limited
$TaskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $TaskAction -Trigger $Trigger -Principal $Principal -Settings $TaskSettings -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$Ready = $false
for ($i = 0; $i -lt 45; $i++) {
    try { $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 2; if ($Health.role -eq $Role) { $Ready = $true; break } } catch { }
    Start-Sleep -Seconds 1
}
if (-not $Ready) { throw "The agent did not become ready. Inspect Scheduled Task $TaskName or run: $RuntimePython -m mcp_transfer --home `"$DataRoot`" $Role --port $Port" }
Write-Host "`n$Role installed. WebUI: http://127.0.0.1:$Port" -ForegroundColor Green
Write-Host 'Local access password (save securely):'
& $RuntimePython -m mcp_transfer --home $DataRoot local-password
$PasswordCmd = "@echo off`r`n`"%LOCALAPPDATA%\MCPTransfer\$Role\runtime\Scripts\python.exe`" -m mcp_transfer --home `"%LOCALAPPDATA%\MCPTransfer\$Role\data`" local-password`r`npause`r`n"
Set-Content -Path (Join-Path $InstallRoot 'show-access-password.cmd') -Value $PasswordCmd -Encoding ASCII
Start-Process "http://127.0.0.1:$Port"
Write-Host 'In the WebUI, enter the cloud URL and the one-time enrollment code for this role.'
