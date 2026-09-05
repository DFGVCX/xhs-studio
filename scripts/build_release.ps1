param(
    [string]$Version = ''
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $taskRoot

if (-not $Version) {
    $taskVersionLine = Select-String -LiteralPath (Join-Path $taskRoot 'xhs_console/__init__.py') -Pattern '__version__\s*=\s*"([^"]+)"'
    if (-not $taskVersionLine) { throw 'Cannot read the application version.' }
    $Version = $taskVersionLine.Matches[0].Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid release version: $Version" }

$taskBundleLockPath = Join-Path $taskRoot 'scripts/browser-bundle.lock.json'
$taskBundleLock = Get-Content -LiteralPath $taskBundleLockPath -Raw | ConvertFrom-Json
if ($taskBundleLock.platform -ne 'win64' -or $taskBundleLock.version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    throw 'The bundled browser lock file is invalid.'
}
$taskBundleDir = Join-Path $taskRoot "build/browser-cache/$($taskBundleLock.version)"
$taskDownloadDir = Join-Path $taskRoot "build/browser-downloads/$($taskBundleLock.version)"
New-Item -ItemType Directory -Path $taskBundleDir,$taskDownloadDir -Force | Out-Null

function Get-LockedArchive {
    param(
        [Parameter(Mandatory = $true)] [string]$Name,
        [Parameter(Mandatory = $true)] [string]$Url,
        [Parameter(Mandatory = $true)] [string]$Sha256
    )
    $taskArchivePath = Join-Path $taskDownloadDir $Name
    $taskValid = (Test-Path -LiteralPath $taskArchivePath) -and
        ((Get-FileHash -LiteralPath $taskArchivePath -Algorithm SHA256).Hash -eq $Sha256)
    if (-not $taskValid) {
        $taskPartialPath = "$taskArchivePath.partial"
        if (Test-Path -LiteralPath $taskPartialPath) { Remove-Item -LiteralPath $taskPartialPath -Force }
        Write-Host "Downloading locked browser asset: $Name"
        Invoke-WebRequest -Uri $Url -OutFile $taskPartialPath
        $taskActualHash = (Get-FileHash -LiteralPath $taskPartialPath -Algorithm SHA256).Hash
        if ($taskActualHash -ne $Sha256) {
            Remove-Item -LiteralPath $taskPartialPath -Force
            throw "SHA256 verification failed for $Name"
        }
        Move-Item -LiteralPath $taskPartialPath -Destination $taskArchivePath -Force
    }
    return $taskArchivePath
}

$taskChromeZip = Get-LockedArchive -Name 'chrome-win64.zip' -Url $taskBundleLock.chrome.url -Sha256 $taskBundleLock.chrome.sha256
$taskDriverZip = Get-LockedArchive -Name 'chromedriver-win64.zip' -Url $taskBundleLock.chromedriver.url -Sha256 $taskBundleLock.chromedriver.sha256
$taskChromeExe = Join-Path $taskBundleDir 'chrome-win64/chrome.exe'
$taskDriverExe = Join-Path $taskBundleDir 'chromedriver-win64/chromedriver.exe'
if (-not (Test-Path -LiteralPath $taskChromeExe)) {
    Expand-Archive -LiteralPath $taskChromeZip -DestinationPath $taskBundleDir -Force
}
if (-not (Test-Path -LiteralPath $taskDriverExe)) {
    Expand-Archive -LiteralPath $taskDriverZip -DestinationPath $taskBundleDir -Force
}
if (-not (Test-Path -LiteralPath $taskChromeExe) -or -not (Test-Path -LiteralPath $taskDriverExe)) {
    throw 'The bundled browser could not be prepared.'
}
$env:XHS_BUNDLED_BROWSER_DIR = $taskBundleDir

$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    python -m venv (Join-Path $taskRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required to build the release.' }
}

& $taskPython -m pip install -r (Join-Path $taskRoot 'requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }

& $taskPython -m PyInstaller --noconfirm --clean (Join-Path $taskRoot 'xhs-studio.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }

$taskPackage = Join-Path $taskRoot 'dist/XHS-Studio'
if (-not (Test-Path -LiteralPath (Join-Path $taskPackage 'XHS-Studio.exe'))) {
    throw 'The packaged executable was not produced.'
}
Copy-Item -LiteralPath (Join-Path $taskRoot 'README.md') -Destination $taskPackage -Force
Copy-Item -LiteralPath (Join-Path $taskRoot 'LICENSE') -Destination $taskPackage -Force
Copy-Item -LiteralPath (Join-Path $taskRoot 'SECURITY.md') -Destination $taskPackage -Force
Set-Content -LiteralPath (Join-Path $taskPackage 'BUNDLED_BROWSER.txt') -Value "Chrome for Testing $($taskBundleLock.version) ($($taskBundleLock.platform))`r`nhttps://googlechromelabs.github.io/chrome-for-testing/" -Encoding utf8

$taskArchive = Join-Path $taskRoot "dist/XHS-Studio-Windows-x64-$Version.zip"
if (Test-Path -LiteralPath $taskArchive) { Remove-Item -LiteralPath $taskArchive -Force }
Compress-Archive -Path (Join-Path $taskPackage '*') -DestinationPath $taskArchive -CompressionLevel Optimal
$taskHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
$taskHashFile = "$taskArchive.sha256"
Set-Content -LiteralPath $taskHashFile -Value "$taskHash  $([System.IO.Path]::GetFileName($taskArchive))" -Encoding ascii

Write-Output "Release archive: $taskArchive"
Write-Output "SHA256: $taskHash"
