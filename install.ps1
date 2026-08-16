[CmdletBinding()]
param(
    [ValidateSet("Local", "GitHub")]
    [string]$Source,
    [ValidatePattern('^v?\d+\.\d+\.\d+$')]
    [string]$Version
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python -ErrorAction Stop).Source
$installRoot = Join-Path $env:LOCALAPPDATA "Programs\enm"
$binDir = Join-Path $installRoot "bin"
$launcher = Join-Path $binDir "enm.cmd"
$releaseApi = "https://api.github.com/repos/MRoldL001/ENM/releases?per_page=100"

function Show-Banner {
    $encoded = "4paI4paI4paI4paI4paI4paI4paI4pWX4paI4paI4paI4pWXICAg4paI4paI4pWX4paI4paI4paI4pWXICAg4paI4paI4paI4pWXCuKWiOKWiOKVlOKVkOKVkOKVkOKVkOKVneKWiOKWiOKWiOKWiOKVlyAg4paI4paI4pWR4paI4paI4paI4paI4pWXIOKWiOKWiOKWiOKWiOKVkQrilojilojilojilojilojilZcgIOKWiOKWiOKVlOKWiOKWiOKVlyDilojilojilZHilojilojilZTilojilojilojilojilZTilojilojilZEK4paI4paI4pWU4pWQ4pWQ4pWdICDilojilojilZHilZrilojilojilZfilojilojilZHilojilojilZHilZrilojilojilZTilZ3ilojilojilZEK4paI4paI4paI4paI4paI4paI4paI4pWX4paI4paI4pWRIOKVmuKWiOKWiOKWiOKWiOKVkeKWiOKWiOKVkSDilZrilZDilZ0g4paI4paI4pWRCuKVmuKVkOKVkOKVkOKVkOKVkOKVkOKVneKVmuKVkOKVnSAg4pWa4pWQ4pWQ4pWQ4pWd4pWa4pWQ4pWdICAgICDilZrilZDilZ0K"
    $banner = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
    Write-Host $banner -ForegroundColor Cyan
}

function Select-InstallSource {
    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw "Interactive selection requires a terminal. Use -Source Local or -Source GitHub."
    }
    $options = @(
        "Install ENM from this folder",
        "Download ENM from GitHub Releases"
    )
    $selected = 0
    $top = [Console]::CursorTop
    [Console]::CursorVisible = $false
    try {
        while ($true) {
            [Console]::SetCursorPosition(0, $top)
            Write-Host "Choose an installation source:" -ForegroundColor White
            for ($index = 0; $index -lt $options.Count; $index++) {
                $prefix = if ($index -eq $selected) { ">" } else { " " }
                $color = if ($index -eq $selected) { "Cyan" } else { "Gray" }
                Write-Host ("{0} {1}" -f $prefix, $options[$index]).PadRight(64) -ForegroundColor $color
            }
            Write-Host "Use Up/Down arrows and Enter.".PadRight(64) -ForegroundColor DarkGray
            $key = [Console]::ReadKey($true).Key
            switch ($key) {
                "UpArrow" { $selected = ($selected - 1 + $options.Count) % $options.Count }
                "DownArrow" { $selected = ($selected + 1) % $options.Count }
                "Enter" { return @("Local", "GitHub")[$selected] }
                "Escape" { throw "Installation cancelled." }
            }
        }
    } finally {
        [Console]::CursorVisible = $true
    }
}

function Select-Release {
    param([Parameter(Mandatory)] [object[]]$Releases)
    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        throw "Release selection requires a terminal. Pass -Version X.Y.Z."
    }
    $selected = 0
    $top = [Console]::CursorTop
    [Console]::CursorVisible = $false
    try {
        while ($true) {
            [Console]::SetCursorPosition(0, $top)
            Write-Host "Choose an ENM version:" -ForegroundColor White
            for ($index = 0; $index -lt $Releases.Count; $index++) {
                $release = $Releases[$index]
                $prefix = if ($index -eq $selected) { ">" } else { " " }
                $color = if ($index -eq $selected) { "Cyan" } else { "Gray" }
                $suffix = if ($release.prerelease) { " (pre-release)" } else { "" }
                Write-Host ("{0} {1}{2}" -f $prefix, $release.tag_name, $suffix).PadRight(64) -ForegroundColor $color
            }
            Write-Host "Use Up/Down arrows and Enter.".PadRight(64) -ForegroundColor DarkGray
            $key = [Console]::ReadKey($true).Key
            switch ($key) {
                "UpArrow" { $selected = ($selected - 1 + $Releases.Count) % $Releases.Count }
                "DownArrow" { $selected = ($selected + 1) % $Releases.Count }
                "Enter" { return $Releases[$selected] }
                "Escape" { throw "Installation cancelled." }
            }
        }
    } finally {
        [Console]::CursorVisible = $true
    }
}

function Invoke-WithClover {
    param(
        [Parameter(Mandatory)] [string]$Message,
        [Parameter(Mandatory)] [scriptblock]$Action,
        [object[]]$Arguments = @()
    )
    $frames = @(
        ".",
        [string][char]0x00B7,
        "+",
        [string][char]0x2723,
        [string][char]0x2724,
        [string][char]0x2723,
        "+",
        [string][char]0x00B7
    )
    $job = Start-Job -ScriptBlock $Action -ArgumentList $Arguments
    $index = 0
    try {
        while ($job.State -in @("NotStarted", "Running")) {
            Write-Host ("`r{0}" -f $frames[$index]) -NoNewline -ForegroundColor Blue
            Write-Host (" {0}" -f $Message) -NoNewline
            $index = ($index + 1) % $frames.Count
            Start-Sleep -Milliseconds 110
            $job = Get-Job -Id $job.Id
        }
        Write-Host ("`r" + (" " * ($Message.Length + 4)) + "`r") -NoNewline
        Receive-Job -Job $job -Wait -ErrorAction SilentlyContinue
        if ($job.State -ne "Completed") {
            $reason = $job.ChildJobs[0].JobStateInfo.Reason
            if ($reason) { throw $reason }
            throw "$Message failed."
        }
    } finally {
        Write-Host ("`r" + (" " * ($Message.Length + 4)) + "`r") -NoNewline
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
    }
}

function Get-LocalPackage {
    param([string]$Root = $projectRoot)
    $projectFile = Join-Path $Root "pyproject.toml"
    $required = @(
        $projectFile,
        (Join-Path $Root "src\enm\__init__.py"),
        (Join-Path $Root "src\enm\cli.py")
    )
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($missing) {
        throw "This folder is not an ENM source package. Missing: $($missing -join ', ')"
    }
    $text = [System.IO.File]::ReadAllText($projectFile)
    $nameMatch = [regex]::Match($text, '(?ms)^\[project\].*?^name\s*=\s*"enm"')
    $versionMatch = [regex]::Match(
        $text,
        '(?ms)^\[project\].*?^version\s*=\s*"(?<version>\d+\.\d+\.\d+)"'
    )
    if (-not $nameMatch.Success -or -not $versionMatch.Success) {
        throw "pyproject.toml does not describe a supported ENM package."
    }
    return [pscustomobject]@{
        Version = $versionMatch.Groups["version"].Value
        InstallTarget = $Root
        TemporaryRoot = $null
    }
}

function Get-GitHubPackage {
    param([string]$RequestedVersion)
    $headers = @{
        Accept = "application/vnd.github+json"
        "User-Agent" = "enm-installer/0.1"
        "X-GitHub-Api-Version" = "2022-11-28"
    }
    try {
        $releases = @(Invoke-WithClover "Checking GitHub Releases" {
            param($Uri, $Headers)
            $ErrorActionPreference = "Stop"
            $response = Invoke-RestMethod -Uri $Uri -Headers $Headers
            foreach ($release in @($response)) {
                Write-Output $release
            }
        } @($releaseApi, $headers))
    } catch {
        throw "Could not read ENM Releases. No published Release was found, or GitHub could not be reached."
    }
    $releases = @($releases | Where-Object { $_ -and $_.tag_name -and -not $_.draft })
    if (-not $releases.Count) {
        throw "No published ENM Release was found."
    }
    if ($RequestedVersion) {
        $wantedTag = if ($RequestedVersion.StartsWith("v")) { $RequestedVersion } else { "v$RequestedVersion" }
        $release = @($releases | Where-Object { $_.tag_name -eq $wantedTag }) | Select-Object -First 1
        if (-not $release) {
            throw "ENM Release $wantedTag was not found."
        }
    } else {
        $release = Select-Release -Releases $releases
    }
    $versionMatch = [regex]::Match("$($release.tag_name)", '^v?(?<version>\d+\.\d+\.\d+)$')
    if (-not $versionMatch.Success) {
        throw "Release tag '$($release.tag_name)' is not a supported ENM version."
    }
    if ("$($release.zipball_url)" -notmatch '^https://api\.github\.com/repos/MRoldL001/ENM/zipball/') {
        throw "Release $($release.tag_name) has an unexpected source archive URL."
    }
    $temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("enm-install-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $temporaryRoot | Out-Null
    $archive = Join-Path $temporaryRoot "source.zip"
    $extractRoot = Join-Path $temporaryRoot "source"
    try {
        Invoke-WithClover "Downloading ENM $($versionMatch.Groups['version'].Value)" {
            param($Uri, $Destination, $Headers)
            $ErrorActionPreference = "Stop"
            Invoke-WebRequest -Uri $Uri -Headers $Headers -OutFile $Destination
        } @($release.zipball_url, $archive, $headers)
        Invoke-WithClover "Extracting ENM source" {
            param($Archive, $Destination)
            $ErrorActionPreference = "Stop"
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $zip = [System.IO.Compression.ZipFile]::OpenRead($Archive)
            try {
                foreach ($entry in $zip.Entries) {
                    $name = $entry.FullName.Replace("\", "/")
                    if ($name.StartsWith("/") -or $name -match '^[A-Za-z]:' -or ($name -split "/") -contains "..") {
                        throw "The source archive contains an unsafe path: $name"
                    }
                }
            } finally {
                $zip.Dispose()
            }
            Expand-Archive -LiteralPath $Archive -DestinationPath $Destination
        } @($archive, $extractRoot)
        $sourceFolders = @(Get-ChildItem -LiteralPath $extractRoot -Directory)
        if ($sourceFolders.Count -ne 1) {
            throw "The GitHub source archive has an unexpected directory structure."
        }
        $package = Get-LocalPackage -Root $sourceFolders[0].FullName
        if ($package.Version -ne $versionMatch.Groups["version"].Value) {
            throw "Release tag $($release.tag_name) does not match package version $($package.Version)."
        }
        $package.TemporaryRoot = $temporaryRoot
        return $package
    } catch {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

function Assert-NotDowngrade {
    param([Parameter(Mandatory)] [string]$Candidate)
    $installedText = & $python -c "import importlib.metadata as m; print(m.version('enm')) if any(d.metadata.get('Name','').lower() == 'enm' for d in m.distributions()) else None"
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect the installed ENM version."
    }
    $installedText = "$installedText".Trim()
    if ($installedText -and ([version]$Candidate -lt [version]$installedText)) {
        throw "Refusing to downgrade ENM from $installedText to $Candidate."
    }
}

Show-Banner
if (-not $Source) {
    $Source = Select-InstallSource
}

$package = $null
$installError = $null
try {
    if ($Source -eq "Local" -and $Version) {
        throw "-Version can only be used with -Source GitHub."
    }
    $package = if ($Source -eq "Local") { Get-LocalPackage } else { Get-GitHubPackage -RequestedVersion $Version }
    Assert-NotDowngrade $package.Version
    Write-Host "Installing ENM $($package.Version) from $Source source."
    Invoke-WithClover "Installing ENM $($package.Version)" {
        param($Python, $Target)
        # pip writes ordinary warnings to stderr; rely on its exit code instead.
        $ErrorActionPreference = "Continue"
        & $Python -m pip install --disable-pip-version-check --no-warn-script-location --upgrade $Target 2>&1 |
            ForEach-Object { "$_" }
        $pipExit = $LASTEXITCODE
        if ($pipExit -ne 0) { throw "pip failed with exit code $pipExit" }
    } @($python, $package.InstallTarget)

    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $launcherContent = "@echo off`r`n`"$python`" -m enm %*`r`n"
    [System.IO.File]::WriteAllText($launcher, $launcherContent, [System.Text.Encoding]::ASCII)

    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $entries = @($userPath -split ";" | Where-Object { $_ })
    $alreadyPresent = $entries | Where-Object {
        [string]::Equals($_.TrimEnd("\"), $binDir.TrimEnd("\"), [StringComparison]::OrdinalIgnoreCase)
    }
    if (-not $alreadyPresent) {
        [Environment]::SetEnvironmentVariable("Path", ((@($entries) + $binDir) -join ";"), "User")
        Write-Host "Added $binDir to the user PATH."
    }
    if (-not (($env:Path -split ";") -contains $binDir)) {
        $env:Path = "$env:Path;$binDir"
    }

    & $launcher --version
    Write-Host "Installation complete. Open a new terminal, then run: enm doctor" -ForegroundColor Green
} catch {
    $installError = $_.Exception.Message
} finally {
    if ($package -and $package.TemporaryRoot -and (Test-Path -LiteralPath $package.TemporaryRoot)) {
        Remove-Item -LiteralPath $package.TemporaryRoot -Recurse -Force
    }
}

if ($installError) {
    Write-Host "ERROR: $installError" -ForegroundColor Red
    exit 1
}
