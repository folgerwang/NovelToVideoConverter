# Install a CUDA-enabled torch into a venv, and prove it actually works.
#
# On Windows the plain PyPI wheel (pip install torch) is the CPU build --
# that is why ComfyUI died with "Torch not compiled with CUDA enabled".
# CUDA builds only come from PyTorch's own index, one index per CUDA
# version. Rather than hardcode one that may be retired, try them in
# order and keep the first that reports torch.cuda.is_available().

param(
    [Parameter(Mandatory = $true)][string]$Venv,
    [string[]]$Cuda = @()
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "  x no python at $py - create the venv first"
    exit 1
}

function Test-Cuda {
    & $py -c "import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)" 2>$null
    return ($LASTEXITCODE -eq 0)
}

if (Test-Cuda) {
    $ver = (& $py -c "import torch;print(torch.__version__)" 2>$null)
    Write-Host "  [torch] CUDA already working ($ver) - nothing to do"
    exit 0
}

# Prefer the CUDA series the installed driver advertises, then walk back.
if (-not $Cuda -or $Cuda.Count -eq 0) {
    $Cuda = @("cu130", "cu129", "cu128", "cu126")
    $smi = (& nvidia-smi 2>$null | Out-String)
    if ($smi -match "CUDA Version:\s*(\d+)\.(\d+)") {
        $pref = "cu$($Matches[1])$($Matches[2])"
        Write-Host "  [torch] driver reports CUDA $($Matches[1]).$($Matches[2])"
        $Cuda = @($pref) + ($Cuda | Where-Object { $_ -ne $pref })
    }
}

Write-Host "  [torch] removing the CPU-only build ..."
& $py -m pip uninstall -y torch torchvision torchaudio 2>&1 | Out-Null

foreach ($cu in $Cuda) {
    $url = "https://download.pytorch.org/whl/$cu"
    Write-Host "  [torch] trying $cu ..."
    & $py -m pip install -q torch torchvision --index-url $url
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  .  $cu has no wheel for this Python - next"
        continue
    }
    if (Test-Cuda) {
        $ver = (& $py -c "import torch;print(torch.__version__)" 2>$null)
        $gpu = (& $py -c "import torch;print(torch.cuda.get_device_name(0))" 2>$null)
        Write-Host "  +  torch $ver on $cu - CUDA OK ($gpu)"
        exit 0
    }
    Write-Host "  .  $cu installed but CUDA still unavailable - next"
    & $py -m pip uninstall -y torch torchvision 2>&1 | Out-Null
}

Write-Host ""
Write-Host "  x Could not get a CUDA-enabled torch for this Python."
Write-Host "    Python 3.13 is new enough that a CUDA wheel may not exist yet."
Write-Host "    Two ways out:"
Write-Host "      - install Python 3.12, delete venvs\comfy, run Install again"
Write-Host "      - check https://pytorch.org/get-started/locally/ for the"
Write-Host "        current Windows CUDA index and set it in scripts\env.bat"
exit 1
