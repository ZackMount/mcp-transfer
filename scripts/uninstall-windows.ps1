param([Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role)
$ErrorActionPreference = 'Stop'
$TaskName = "MCPTransfer-$Role"
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Stopped and removed $TaskName. Credentials and configuration were preserved in $env:LOCALAPPDATA\MCPTransfer\$Role. Revoke the device in the cloud before removing its files."
