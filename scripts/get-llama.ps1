# Fetch prebuilt llama.cpp CUDA binaries for Windows.
#
# Note: the repo's "latest" release is a marker tag (v0.2.0) that carries no
# binaries -- the real builds live on the b##### tags. So walk recent releases
# and take the newest one that actually has a win-cuda asset.

param(
    [string]$Dest = "$PSScriptRoot\..\llama",
    [string]$Cuda = "12.4"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$headers = @{ "User-Agent" = "bookreel" }

Write-Host "  [llama.cpp] looking for the newest win-cuda-$Cuda build ..."

try {
    $rels = Invoke-RestMethod -Headers $headers `
        -Uri "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30"
} catch {
    Write-Host "  x could not reach the GitHub API: $($_.Exception.Message)"
    exit 1
}

$tag = $null; $binUrl = $null; $rtUrl = $null
foreach ($r in $rels) {
    if ($r.tag_name -notmatch '^b\d+$') { continue }
    $bin = $r.assets | Where-Object { $_.name -eq "llama-$($r.tag_name)-bin-win-cuda-$Cuda-x64.zip" }
    if ($bin) {
        $tag = $r.tag_name
        $binUrl = $bin.browser_download_url
        $rt = $r.assets | Where-Object { $_.name -eq "cudart-llama-bin-win-cuda-$Cuda-x64.zip" }
        if ($rt) { $rtUrl = $rt.browser_download_url }
        break
    }
}

if (-not $binUrl) {
    Write-Host "  x no win-cuda-$Cuda x64 build in the last 30 releases."
    Write-Host "    Try a different CUDA version: set LLAMA_CUDA in scripts\env.bat"
    Write-Host "    (13.3 is the other one currently published)."
    exit 1
}

$Dest = [System.IO.Path]::GetFullPath($Dest)
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$stamp  = Join-Path $Dest ".build"
$server = Join-Path $Dest "llama-server.exe"

if ((Test-Path $stamp) -and (Test-Path $server) -and ((Get-Content $stamp -Raw).Trim() -eq $tag)) {
    Write-Host "  [llama.cpp] $tag already installed"
    exit 0
}

$tmp = Join-Path $env:TEMP "bookreel-llama"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    Write-Host "  [llama.cpp] downloading $tag ..."
    $z = Join-Path $tmp "llama.zip"
    Invoke-WebRequest -Headers $headers -Uri $binUrl -OutFile $z
    Expand-Archive -Path $z -DestinationPath $Dest -Force

    if ($rtUrl) {
        Write-Host "  [llama.cpp] downloading the CUDA runtime DLLs ..."
        $z2 = Join-Path $tmp "cudart.zip"
        Invoke-WebRequest -Headers $headers -Uri $rtUrl -OutFile $z2
        Expand-Archive -Path $z2 -DestinationPath $Dest -Force
    }
} catch {
    Write-Host "  x download failed: $($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# Some builds nest everything one folder deep - flatten so the paths in
# run-llm.bat stay stable.
if (-not (Test-Path $server)) {
    $found = Get-ChildItem -Path $Dest -Recurse -Filter "llama-server.exe" |
             Select-Object -First 1
    if ($found) {
        Get-ChildItem -Path $found.DirectoryName | Move-Item -Destination $Dest -Force
    }
}

if (-not (Test-Path $server)) {
    Write-Host "  x llama-server.exe is not there after extracting - check $Dest"
    exit 1
}

Set-Content -Path $stamp -Value $tag
Write-Host "  [llama.cpp] ready: $server  ($tag)"
exit 0
