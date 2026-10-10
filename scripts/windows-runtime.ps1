function Stop-McpAgent {
    param(
        [Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role,
        [Parameter(Mandatory=$true)][string]$DataRoot,
        [switch]$SkipScheduledTask
    )
    if (-not $SkipScheduledTask) {
        Stop-ScheduledTask -TaskName "MCPTransfer-$Role" -ErrorAction SilentlyContinue
    }
    # Match this user's exact profile and role, including old venv launcher children.
    $OwnSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $HomePattern = '--home\s+(?:"' + [regex]::Escape($DataRoot) + '"|' + [regex]::Escape($DataRoot) + ')\s+' + $Role + '(?:\s|$)'
    $ProcessFilter = "Name='python.exe' OR Name='pythonw.exe' OR Name='mcp-transfer.exe'"
    if ($Role -eq 'host') { $ProcessFilter += " OR Name='frpc.exe'" }
    foreach ($Process in Get-CimInstance Win32_Process -Filter $ProcessFilter) {
        $IsAgent = $Process.CommandLine -match 'mcp[_-]transfer' -and $Process.CommandLine -match $HomePattern
        $IsOwnFrpc = $Role -eq 'host' -and $Process.Name -eq 'frpc.exe' -and $Process.CommandLine -like ('*' + (Join-Path $DataRoot 'frpc.toml') + '*')
        if ($IsAgent -or $IsOwnFrpc) {
            $Owner = Invoke-CimMethod -InputObject $Process -MethodName GetOwnerSid -ErrorAction SilentlyContinue
            if ($null -ne $Owner -and $Owner.ReturnValue -eq 0 -and $Owner.Sid -eq $OwnSid) {
                Stop-Process -Id $Process.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
}
