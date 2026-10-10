param(
    [Parameter(Mandatory=$true)][ValidateSet('host','client')][string]$Role,
    [int]$Port = 0,
    [switch]$AllowInsecureHttp,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'This bootstrap requires Windows.' }
$Stage = Join-Path ([System.IO.Path]::GetTempPath()) ('mcp-transfer-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Stage | Out-Null
try {
    $Archive = Join-Path $Stage 'source.zip'
    Invoke-WebRequest -UseBasicParsing -Uri 'https://codeload.github.com/ZackMount/mcp-transfer/zip/refs/heads/main' -OutFile $Archive
    $Digest = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.Substring(0,16).ToLowerInvariant()
    $Expanded = Join-Path $Stage 'expanded'
    Expand-Archive -LiteralPath $Archive -DestinationPath $Expanded
    $Roots = @(Get-ChildItem -LiteralPath $Expanded -Directory)
    if ($Roots.Count -ne 1 -or -not (Test-Path (Join-Path $Roots[0].FullName 'pyproject.toml'))) { throw 'Invalid source archive.' }
    $SourceRoot = Join-Path $env:LOCALAPPDATA "MCPTransfer-source\$Digest"
    if (-not (Test-Path $SourceRoot)) {
        New-Item -ItemType Directory -Force -Path $SourceRoot | Out-Null
        Get-ChildItem -Force -LiteralPath $Roots[0].FullName | Copy-Item -Destination $SourceRoot -Recurse -Force
    }
    Write-Host "Source ready: $SourceRoot"
    if ($PrepareOnly) { return }
    $Options = @{ Role = $Role; Port = $Port }
    if ($AllowInsecureHttp) { $Options.AllowInsecureHttp = $true }
    & (Join-Path $SourceRoot 'scripts\install-windows.ps1') @Options
} finally {
    if (Test-Path $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
}
