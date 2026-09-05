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

$taskArchive = Join-Path $taskRoot "dist/XHS-Studio-Windows-x64-$Version.zip"
if (Test-Path -LiteralPath $taskArchive) { Remove-Item -LiteralPath $taskArchive -Force }
Compress-Archive -Path (Join-Path $taskPackage '*') -DestinationPath $taskArchive -CompressionLevel Optimal
$taskHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
$taskHashFile = "$taskArchive.sha256"
Set-Content -LiteralPath $taskHashFile -Value "$taskHash  $([System.IO.Path]::GetFileName($taskArchive))" -Encoding ascii

Write-Output "Release archive: $taskArchive"
Write-Output "SHA256: $taskHash"
