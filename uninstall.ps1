[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$python = (Get-Command python -ErrorAction Stop).Source
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\enm"
$binDir = Join-Path $installRoot "bin"

& $python -m pip uninstall -y enm

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$entries = @($userPath -split ";" | Where-Object {
    $_ -and -not [string]::Equals(
        $_.TrimEnd("\"),
        $binDir.TrimEnd("\"),
        [StringComparison]::OrdinalIgnoreCase
    )
})
[Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")

if (Test-Path -LiteralPath $installRoot) {
    Remove-Item -LiteralPath $installRoot -Recurse -Force
}

Write-Host "ENM was removed. Open a new terminal to refresh PATH."
