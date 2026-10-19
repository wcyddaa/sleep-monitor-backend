$ErrorActionPreference = "Stop"

$scriptDir = $PSScriptRoot
$root      = Split-Path -Parent $scriptDir
$staging   = Join-Path $scriptDir "_code_staging"
$dist      = Join-Path $scriptDir "dist"

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Force -Path $dist    | Out-Null
New-Item -ItemType Directory -Force -Path $staging | Out-Null

# 1) copy app source
Copy-Item (Join-Path $root "main.py") $staging -Force
Copy-Item (Join-Path $root "app") (Join-Path $staging "app") -Recurse -Force

# 2) copy bootstrap
Copy-Item (Join-Path $scriptDir "bootstrap") $staging -Force

# 3) CA cert -> ca.pem at package root (cert needs no password)
$caCandidates = @(
    (Join-Path $root      "isrgrootx1.pem"),
    (Join-Path $scriptDir "isrgrootx1.pem"),
    (Join-Path $root      "ca.pem"),
    (Join-Path $scriptDir "ca.pem")
)
$caFile = $caCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($caFile) { Copy-Item $caFile (Join-Path $staging "ca.pem") -Force }

# 4) clean caches
Get-ChildItem (Join-Path $staging "app") -Recurse -Directory -Filter "__pycache__" |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $staging -Recurse -File -Filter "*.pyc" |
    Remove-Item -Force -ErrorAction SilentlyContinue

# 5) LF line endings + no BOM for bootstrap
$bootstrapPath = Join-Path $staging "bootstrap"
$text = [System.IO.File]::ReadAllText($bootstrapPath)
$text = $text -replace "`r`n", "`n"
[System.IO.File]::WriteAllText($bootstrapPath, $text, (New-Object System.Text.UTF8Encoding($false)))

# 6) create code.zip with forward-slash entry paths (Compress-Archive uses
#    backslashes, which breaks import on Linux)
$codeZip = Join-Path $dist "code.zip"
if (Test-Path $codeZip) { Remove-Item $codeZip -Force }

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$zipStream   = [System.IO.File]::Open($codeZip, [System.IO.FileMode]::Create)
$archive     = New-Object System.IO.Compression.ZipArchive($zipStream, [System.IO.Compression.ZipArchiveMode]::Create)
$stagingFull = (Resolve-Path $staging).Path

Get-ChildItem -Path $staging -Recurse -File | ForEach-Object {
    $rel   = $_.FullName.Substring($stagingFull.Length + 1).Replace('\', '/')
    $entry = $archive.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Optimal)
    $es    = $entry.Open()
    $fs    = [System.IO.File]::OpenRead($_.FullName)
    $fs.CopyTo($es)
    $fs.Close()
    $es.Close()
}

$archive.Dispose()
$zipStream.Dispose()

# 7) clean staging
Remove-Item $staging -Recurse -Force

Write-Host "Generated code.zip -> $codeZip"