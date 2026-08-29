param([string]$Repository = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not $Repository) {
    $Repository = Read-Host "Repositorio GitHub (owner/repositorio)"
}
$Repository = $Repository.Trim()
if ($Repository -notmatch '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' -or $Repository.Contains('..')) {
    throw "Repositorio invalido. Use owner/repositorio."
}
$config = [ordered]@{
    schema = 1
    repository = $Repository
    channel = "stable"
    auto_update = $true
    check_timeout_seconds = 5
}
$json = $config | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $PSScriptRoot "update-channel.json"), $json + "`n",
                               (New-Object System.Text.UTF8Encoding($false)))
Write-Host "Actualizaciones configuradas desde https://github.com/$Repository/releases" -ForegroundColor Green
