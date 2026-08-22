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
    & $py -c "import torchaudio" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  [torch] CUDA already working ($ver) - nothing to do"
        exit 0
    }
    # torch is fine but torchaudio is not there. Pull it from the index
    # matching the installed build rather than redoing the whole probe.
    Write-Host "  [torch] CUDA works ($ver) but torchaudio is missing"
    $tag = ($ver -split '\+')[1]
    if ($tag) {
        Write-Host "  [torch] installing torchaudio from $tag ..."
        & $py -m pip install -q torchaudio --index-url "https://download.pytorch.org/whl/$tag"
        & $py -c "import torchaudio" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  +  torchaudio installed"
            exit 0
        }
        Write-Host "  !  torchaudio not available on $tag"
    }
    Write-Host "  [torch] reinstalling all three together ..."
}

# Prefer the CUDA series the installed driver advertises, then walk back.
if (-not $Cuda -or $Cuda.Count -eq 0) {
    # PyTorch publishes a handful of specific indexes, not one per driver
    # version -- a driver reporting 13.1 has no cu131 index. CUDA is
    # backward compatible, so keep the published indexes at or below what
    # the driver supports, newest first, and drop the ones above it.
    $known = @("cu130", "cu129", "cu128", "cu126")
    $Cuda = $known
    $smi = (& nvidia-smi 2>$null | Out-String)
    if ($smi -match "CUDA Version:\s*(\d+)\.(\d+)") {
        $major = [int]$Matches[1]; $minor = [int]$Matches[2]
        $driver = $major * 10 + $minor
        Write-Host "  [torch] driver supports CUDA $major.$minor"
        $usable = @($known | Where-Object { [int]($_ -replace 'cu', '') -le $driver })
        if ($usable.Count -gt 0) {
            $Cuda = $usable
            Write-Host "  [torch] will try: $($Cuda -join ', ')"
        } else {
            Write-Host "  [torch] driver is older than every published index - trying all"
        }
    }
}

Write-Host "  [torch] removing the CPU-only build ..."
& $py -m pip uninstall -y torch torchvision torchaudio 2>&1 | Out-Null

foreach ($cu in $Cuda) {
    $url = "https://download.pytorch.org/whl/$cu"
    Write-Host "  [torch] trying $cu ..."

    # All three together, from the same index: ComfyUI imports torchaudio
    # (comfy/ldm/lightricks/vae/audio_vae.py), and the three have to be
    # ABI-matched, so a torchaudio left behind from PyPI is not enough.
    & $py -m pip install -q torch torchvision torchaudio --index-url $url
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  .  $cu is missing one of the three - retrying without torchaudio"
        & $py -m pip install -q torch torchvision --index-url $url
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  .  $cu has no wheel for this Python - next"
        continue
    }
    if (Test-Cuda) {
        $ver = (& $py -c "import torch;print(torch.__version__)" 2>$null)
        $gpu = (& $py -c "import torch;print(torch.cuda.get_device_name(0))" 2>$null)
        Write-Host "  +  torch $ver on $cu - CUDA OK ($gpu)"

        & $py -c "import torchaudio" 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  .  torchaudio missing - ComfyUI needs it, trying $cu ..."
            & $py -m pip install -q torchaudio --index-url $url
            & $py -c "import torchaudio" 2>$null
            if ($LASTEXITCODE -ne 0) {
                Write-Host "  !  torchaudio unavailable on $cu - ComfyUI will fail at"
                Write-Host "     'import torchaudio' in comfy\ldm\lightricks\vae\audio_vae.py"
            } else {
                Write-Host "  +  torchaudio installed"
            }
        }
        exit 0
    }
    Write-Host "  .  $cu installed but CUDA still unavailable - next"
    & $py -m pip uninstall -y torch torchvision torchaudio 2>&1 | Out-Null
}

Write-Host ""
Write-Host "  x Could not get a CUDA-enabled torch for this Python."
Write-Host "    Python 3.13 is new enough that a CUDA wheel may not exist yet."
Write-Host "    Two ways out:"
Write-Host "      - install Python 3.12, delete venvs\comfy, run Install again"
Write-Host "      - check https://pytorch.org/get-started/locally/ for the"
Write-Host "        current Windows CUDA index and set it in scripts\env.bat"
exit 1
