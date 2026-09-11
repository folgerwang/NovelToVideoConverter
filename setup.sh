#!/usr/bin/env bash
# Bookreel - put the Linux stack on disk. launch.sh starts it; this installs it.
#
# Windows has setup.bat and macOS setup.command, and both install *and* start.
# This one only installs. Starting is launch.sh's job and the flags it passes
# are load-bearing - --cache-none, --reserve-vram 16, the two ROCm env vars,
# the stall and restart wiring for slot C - so a second copy of them here would
# be a second copy to get wrong.
#
#     ./setup.sh            what is here and what is missing
#     ./setup.sh all        every step below, in order, skipping what is done
#     ./setup.sh venvs      venvs/tools (the services) and venvs/comfy
#     ./setup.sh torch      the ROCm wheels for this GPU, pinned
#     ./setup.sh comfy      the ComfyUI checkout and the GGUF loader node
#     ./setup.sh weights    what ComfyUI loads: Flux.2 and MiniMax-H3
#     ./setup.sh models     models/: CosyVoice2, embeddings, the CosyVoice code
#     ./setup.sh ollama     pull the two models ollama serves
#     ./setup.sh ffmpeg     bin/ffmpeg out of imageio-ffmpeg
#     ./setup.sh check      the report, and nothing else
#
# Nothing here starts a service, nothing re-downloads a file already on disk
# and nothing rebuilds a venv that works, so re-running it is how a
# half-finished install is repaired. A full install pulls ~90GB of weights.
#
# ASSUME_YES=1 answers the download prompts, for an unattended run.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENVS="$HERE/venvs"
COMFY="$HERE/ComfyUI"
MODELS="$HERE/models"
LOGS="$HERE/logs"
PY_BIN="${PY_BIN:-python3}"
ASSUME_YES="${ASSUME_YES:-0}"

# Keep the Hub's blob cache inside the project. Otherwise ~/.cache fills with a
# second copy of 90GB of weights on a box whose home directory may be small.
export HF_HOME="${HF_HOME:-$MODELS/hf}"

# --- torch for venvs/comfy ------------------------------------------
# gfx1151 (Strix Halo) is not served by download.pytorch.org's rocm channel.
# The wheels that work here come from AMD's per-architecture nightly index and
# carry their own ROCm runtime in rocm-sdk-* packages, so no system ROCm
# install is involved and nothing outside this venv is touched.
#
# All three are pinned to the build this box has actually rendered with. That
# is not tidiness: on 2026-09-10 a text-encoder pass that had taken ~2 minutes
# started never finishing, and ruling the venv out as the cause rested on
# knowing that nothing in it had moved. An unpinned re-install would take
# whatever nightly is newest and throw that answer away.
ROCM_INDEX="https://rocm.nightlies.amd.com/v2/gfx1151/"
ROCM_TORCH="2.8.0+rocm7.13.0a20260512"
ROCM_VISION="0.23.0a0+rocm7.13.0a20260512"
ROCM_AUDIO="2.8.0a0+rocm7.13.0a20260512"

# --- torch for venvs/tools ------------------------------------------
# CPU on purpose. tts, asr and embed all run with --device cpu because the GPU
# belongs to ComfyUI, and a second ROCm torch in this venv would pull another
# copy of the runtime and invite both processes onto the same card.
CPU_INDEX="https://download.pytorch.org/whl/cpu"
CPU_TORCH="2.7.1+cpu"
CPU_AUDIO="2.7.1+cpu"

COMFY_REPO="https://github.com/comfyanonymous/ComfyUI.git"
# UnetLoaderGGUF, which the Flux.2 workflow needs: the fp8mixed UNet is 34GB
# and this machine's host side is 31GiB, so the image slot runs a Q4 GGUF.
GGUF_REPO="https://github.com/city96/ComfyUI-GGUF"
COSYVOICE_REPO="https://github.com/FunAudioLLM/CosyVoice"

M_EMBED="Qwen/Qwen3-Embedding-0.6B"
M_TTS="FunAudioLLM/CosyVoice2-0.5B"

# --- where each weight comes from ------------------------------------
# dir | filename | repo | path inside the repo | megabytes
#
# This table is only an address book: what actually gets fetched is whatever
# launch.sh and comfy-flux2-9x16.json name (see wanted_weights). A file named
# there that is missing here is reported, never guessed at - downloading the
# wrong 20GB file into diffusion_models/ is worse than saying so.
WEIGHTS="
diffusion_models|flux2-dev-Q4_K_S.gguf|city96/FLUX.2-dev-gguf|flux2-dev-Q4_K_S.gguf|18000
text_encoders|mistral_3_small_flux2_fp4_mixed.safetensors|Comfy-Org/flux2-dev|split_files/text_encoders/mistral_3_small_flux2_fp4_mixed.safetensors|11400
text_encoders|mistral_3_small_flux2_fp8.safetensors|Comfy-Org/flux2-dev|split_files/text_encoders/mistral_3_small_flux2_fp8.safetensors|17000
diffusion_models|flux2_dev_fp8mixed.safetensors|Comfy-Org/flux2-dev|split_files/diffusion_models/flux2_dev_fp8mixed.safetensors|34000
vae|flux2-vae.safetensors|Comfy-Org/flux2-dev|split_files/vae/flux2-vae.safetensors|321
loras|Flux_2-Turbo-LoRA_comfyui.safetensors|Comfy-Org/flux2-dev|split_files/loras/Flux_2-Turbo-LoRA_comfyui.safetensors|2600
diffusion_models|minimax_h3_fl2va_pruned_fp8_scaled.safetensors|Comfy-Org/MiniMax-H3|diffusion_models/minimax_h3_fl2va_pruned_fp8_scaled.safetensors|20000
text_encoders|qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|Comfy-Org/MiniMax-H3|text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors|15000
text_encoders|qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors|Momoking/Qwen3-VL-32B-Heretic-MiniMax-H3-NVFP4|qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors|15000
vae|minimax_h3_video_vae_fp16.safetensors|Comfy-Org/MiniMax-H3|vae/minimax_h3_video_vae_fp16.safetensors|4900
vae|minimax_h3_audio_vae_fp32.safetensors|Comfy-Org/MiniMax-H3|vae/minimax_h3_audio_vae_fp32.safetensors|578
loras|minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors|Comfy-Org/MiniMax-H3|loras/minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors|1900
"

# ==========================================================
#  Small helpers
# ==========================================================
say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
fail() { printf '  x %s\n' "$*"; }
head2() { printf '\n  %s\n' "$*"; }
row()  { printf '  %-14s %s\n' "$1" "$2"; }

have() { command -v "$1" >/dev/null 2>&1; }

# has_mod <python> <module> - installed, without importing it. Importing torch
# or funasr just to ask costs seconds, and check asks about a dozen things.
has_mod() {
  "$1" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('$2') else 1)" 2>/dev/null
}

venv_py() { printf '%s/%s/bin/python' "$VENVS" "$1"; }
have_venv() { [ -x "$(venv_py "$1")" ]; }

ask() {
  [ "$ASSUME_YES" = "1" ] && return 0
  local a=""
  printf '  %s [y/N]: ' "$1"
  # A closed stdin (cron, a pipe) answers no, and the newline keeps the
  # "skipped" from landing on the end of the prompt.
  read -r a || printf '\n'
  case "$a" in y|Y) return 0 ;; *) say "skipped"; return 1 ;; esac
}

gb() { awk -v m="$1" 'BEGIN{printf "%.1fGB", m/1024}'; }

# ==========================================================
#  What to install is not decided here
# ==========================================================
# The model filenames belong to launch.sh, and a second copy of them in this
# file is how the two drift apart. Read its defaults out of the file instead:
# only plain NAME="${NAME:-value}" lines are taken and evaluated, so nothing
# else in launch.sh runs.
launch_defaults() {
  local line
  while IFS= read -r line; do
    eval "$line"
  done < <(grep -E '^(LLM_MODEL|VERIFY_MODEL|H3_UNET|H3_CLIP|H3_VAE|H3_AUDIO_VAE|H3_LORA)="\$\{[A-Z0-9_]+:?-[^"]*\}"$' "$HERE/launch.sh" 2>/dev/null)
  # A rename in launch.sh must not turn into an unbound variable here.
  : "${LLM_MODEL:=}" "${VERIFY_MODEL:=}" "${H3_UNET:=}" "${H3_CLIP:=}"
  : "${H3_VAE:=}" "${H3_AUDIO_VAE:=}" "${H3_LORA:=}"
}

# The image side names its weights in the workflow template, because that is
# the file ComfyUI is actually handed. Same rule: read, do not repeat.
workflow_names() {
  "$PY_BIN" - "$HERE/comfy-flux2-9x16.json" <<'PYWF' 2>/dev/null
import json, sys
where = {"unet_name": "diffusion_models", "clip_name": "text_encoders",
         "vae_name": "vae", "lora_name": "loras"}
try:
    wf = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(0)
for node in wf.values():
    if not isinstance(node, dict):
        continue
    for key, value in (node.get("inputs") or {}).items():
        if key in where and isinstance(value, str) and value:
            print("%s|%s" % (where[key], value))
PYWF
}

# Every weight this install needs, as "<dir>|<filename>", deduplicated.
wanted_weights() {
  {
    [ -n "$H3_UNET" ]      && printf 'diffusion_models|%s\n' "$H3_UNET"
    [ -n "$H3_CLIP" ]      && printf 'text_encoders|%s\n' "$H3_CLIP"
    [ -n "$H3_VAE" ]       && printf 'vae|%s\n' "$H3_VAE"
    [ -n "$H3_LORA" ]      && printf 'loras|%s\n' "$H3_LORA"
    # Empty by default: h3's own audio branch is off, and the file is only
    # wanted by someone turning it back on.
    [ -n "$H3_AUDIO_VAE" ] && printf 'vae|%s\n' "$H3_AUDIO_VAE"
    workflow_names
  } | awk 'NF && !seen[$0]++'
}

weight_row() { printf '%s\n' "$WEIGHTS" | awk -F'|' -v n="$1" '$2 == n {print; exit}'; }

# ==========================================================
#  check
# ==========================================================
do_check() {
  launch_defaults
  printf '\n  Bookreel - what is installed\n'

  head2 "toolchain"
  if have "$PY_BIN"; then row python3 "$("$PY_BIN" -V 2>&1)"; else row python3 "MISSING - apt install python3 python3-venv"; fi
  have git    && row git    "$(git --version | awk '{print $3}')"        || row git    "MISSING - apt install git"
  have ollama && row ollama "$(ollama --version 2>/dev/null | awk '/client/{print $NF; exit}')" \
              || row ollama "MISSING - https://ollama.com/download"

  head2 "venvs"
  local pt
  if have_venv tools; then
    pt="$(venv_py tools)"
    local missing=""
    for m in fastapi uvicorn torch funasr soundfile imageio_ffmpeg; do
      has_mod "$pt" "$m" || missing="$missing $m"
    done
    [ -z "$missing" ] && row tools "ok" || row tools "missing:$missing"
    has_mod "$pt" sentence_transformers || row "" "(no sentence-transformers - the embed slot stays down)"
  else
    row tools "MISSING - ./setup.sh venvs"
  fi
  if have_venv comfy; then
    pt="$(venv_py comfy)"
    row comfy "$("$pt" -c 'import torch;print("torch " + torch.__version__)' 2>/dev/null || echo 'no torch - ./setup.sh torch')"
    # timeout: right after a wedged ring is killed, every new HIP init sits in
    # uninterruptible sleep for minutes. A report must not hang there.
    if timeout 120 "$pt" -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
      row "" "GPU visible"
    else
      row "" "! torch cannot see the GPU"
    fi
  else
    row comfy "MISSING - ./setup.sh venvs"
  fi

  head2 "ComfyUI"
  [ -f "$COMFY/main.py" ] && row checkout "ok" || row checkout "MISSING - ./setup.sh comfy"
  [ -f "$COMFY/custom_nodes/ComfyUI-GGUF/nodes.py" ] \
    && row ComfyUI-GGUF "ok" || row ComfyUI-GGUF "MISSING - the GGUF workflow will not load"

  head2 "weights (ComfyUI/models)"
  local line dir name missing_mb=0 r
  while IFS='|' read -r dir name; do
    [ -n "${name:-}" ] || continue
    if [ -f "$COMFY/models/$dir/$name" ]; then
      printf '  ok   %s/%s\n' "$dir" "$name"
    else
      r="$(weight_row "$name")"
      if [ -n "$r" ]; then
        missing_mb=$((missing_mb + $(printf '%s' "$r" | awk -F'|' '{print $5}')))
        printf '  MISS %s/%s  (%s)\n' "$dir" "$name" "$(gb "$(printf '%s' "$r" | awk -F'|' '{print $5}')")"
      else
        printf '  MISS %s/%s  (no source known - see WEIGHTS in this file)\n' "$dir" "$name"
      fi
    fi
  done < <(wanted_weights)
  [ "$missing_mb" -gt 0 ] && row "" "$(gb "$missing_mb") to download - ./setup.sh weights"

  head2 "models/"
  [ -d "$MODELS/cosyvoice2" ] && row cosyvoice2 "ok" || row cosyvoice2 "MISSING - tts stays down"
  [ -d "$HERE/third_party/CosyVoice/cosyvoice" ] && row CosyVoice "ok (checkout)" \
    || row CosyVoice "MISSING - not a pip package, tts stays down"
  [ -d "$MODELS/qwen3-embedding" ] && row embeddings "ok" || row embeddings "absent - the embed slot skips"
  # funasr fetches fa-zh from ModelScope on the first /align, so an empty
  # models/funasr before the first run is correct, not a fault.
  [ -d "$MODELS/funasr" ] && row funasr "ok (cached)" || row funasr "empty - fetched on the first align"

  head2 "voices/"
  local w voice=""
  for w in "$HERE"/voices/*.wav; do
    [ -f "$w" ] && [ -f "${w%.wav}.txt" ] && { voice="$(basename "${w%.wav}")"; break; }
  done
  [ -n "$voice" ] && row "$voice" "ok (wav + transcript)" \
    || row voices "MISSING - CosyVoice2 is zero-shot: it needs name.wav + name.txt"

  head2 "ollama"
  local m
  for m in "$LLM_MODEL" "$VERIFY_MODEL"; do
    [ -n "$m" ] || continue
    if ollama_has "$m"; then row "$m" "ok"; else row "$m" "not pulled - ollama pull $m"; fi
  done

  head2 "this machine"
  [ -x "$HERE/bin/ffmpeg" ] && row ffmpeg "ok (bin/ffmpeg)" || row ffmpeg "MISSING - ./setup.sh ffmpeg"
  local vram
  vram="$(cat /sys/class/drm/card0/device/mem_info_vram_total 2>/dev/null || echo 0)"
  [ "${vram:-0}" -gt 0 ] && row vram "$((vram / 1073741824))GiB (BIOS UMA carve-out)"
  row host "$(free -g | awk '/^Mem:/{print $2"GiB total, "$7"GiB available"}')"
  row disk "$(df -h "$HERE" | awk 'NR==2{print $4" free"}')"

  printf '\n  Install what is missing with ./setup.sh all, then start it with ./launch.sh\n\n'
}

# ollama_has <model[:tag]> - true when the model is pulled. Works with the
# daemon down, which is the normal state while installing: ollama list needs
# a running server, but the manifest it would read is a file on disk.
ollama_has() {
  local ref="$1" name tag store
  name="${ref%%:*}"
  tag="${ref#*:}"
  [ "$tag" = "$ref" ] && tag="latest"
  if (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null; then
    exec 3>&-
    ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${name}\(:${tag}\)\?$" && return 0
    ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${ref}$" && return 0
    return 1
  fi
  store="${OLLAMA_MODELS:-$HOME/.ollama/models}"
  [ -f "$store/manifests/registry.ollama.ai/library/$name/$tag" ]
}

# ==========================================================
#  venvs
# ==========================================================
# Two of them, and the split is deliberate: venvs/comfy holds a ROCm torch and
# nothing else may disturb it, venvs/tools holds a CPU torch plus the whole
# CosyVoice dependency tree. One venv for both means one pip resolve deciding
# which torch the GPU gets.
do_venvs() {
  have "$PY_BIN" || { fail "no $PY_BIN - apt install python3 python3-venv"; return 1; }
  local v
  for v in tools comfy; do
    if have_venv "$v"; then say "venvs/$v already here"; continue; fi
    say "creating venvs/$v"
    "$PY_BIN" -m venv "$VENVS/$v" || { fail "venv creation failed - apt install python3-venv"; return 1; }
    "$(venv_py "$v")" -m pip install -qU pip wheel || warn "pip self-upgrade failed in $v"
  done

  local pt; pt="$(venv_py tools)"
  say "venvs/tools: the services and their CPU torch"
  "$pt" -m pip install -q --index-url "$CPU_INDEX" "torch==$CPU_TORCH" "torchaudio==$CPU_AUDIO" \
    || warn "CPU torch install failed"
  # imageio-ffmpeg is here for bin/ffmpeg; huggingface_hub for the downloads
  # below; the rest is what the four services import.
  "$pt" -m pip install -q -U fastapi uvicorn python-multipart soundfile pillow requests \
      funasr modelscope imageio-ffmpeg "huggingface_hub[cli]" \
    || warn "some venvs/tools packages failed"
  say "done. sentence-transformers is left out on purpose: install it only if"
  say "you want the embed slot, which no console page calls yet."
}

# ==========================================================
#  torch (venvs/comfy)
# ==========================================================
do_torch() {
  have_venv comfy || { fail "venvs/comfy missing - ./setup.sh venvs first"; return 1; }
  local pt; pt="$(venv_py comfy)"
  local now=""
  now="$("$pt" -c 'import torch;print(torch.__version__)' 2>/dev/null)"
  if [ "$now" = "$ROCM_TORCH" ]; then say "torch $now already installed"; return 0; fi
  [ -n "$now" ] && warn "replacing torch $now with the pinned $ROCM_TORCH"
  say "torch $ROCM_TORCH from $ROCM_INDEX"
  say "About 4GB: the wheels carry their own ROCm runtime."
  ask "Install now?" || return 0
  "$pt" -m pip install --index-url "$ROCM_INDEX" \
      "torch==$ROCM_TORCH" "torchvision==$ROCM_VISION" "torchaudio==$ROCM_AUDIO" \
    || { fail "install failed - the nightly index prunes old builds; pick a"
         fail "newer trio from ${ROCM_INDEX}torch/ and update the pins above"; return 1; }
  timeout 120 "$pt" -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null \
    && say "GPU visible" || warn "torch is installed but cannot see the GPU yet"
}

# ==========================================================
#  ComfyUI
# ==========================================================
do_comfy() {
  have git || { fail "no git"; return 1; }
  have_venv comfy || { fail "venvs/comfy missing - ./setup.sh venvs first"; return 1; }
  local pt; pt="$(venv_py comfy)"
  if [ -f "$COMFY/main.py" ]; then
    say "ComfyUI already at $COMFY"
  else
    say "cloning ComfyUI"
    git clone "$COMFY_REPO" "$COMFY" || { fail "clone failed"; return 1; }
  fi
  if [ -f "$COMFY/custom_nodes/ComfyUI-GGUF/nodes.py" ]; then
    say "ComfyUI-GGUF already here"
  else
    say "cloning ComfyUI-GGUF (UnetLoaderGGUF, which the Flux.2 workflow needs)"
    git clone "$GGUF_REPO" "$COMFY/custom_nodes/ComfyUI-GGUF" || warn "GGUF node clone failed"
  fi
  # Order matters: torch first, requirements second. ComfyUI's requirements.txt
  # lists torch/torchvision/torchaudio unpinned, so with the ROCm wheels
  # already in place pip leaves them alone - run it on an empty venv and it
  # installs PyPI's CUDA build over them instead.
  "$pt" -c 'import torch' 2>/dev/null || { fail "install torch first: ./setup.sh torch"; return 1; }
  say "ComfyUI requirements"
  "$pt" -m pip install -q -r "$COMFY/requirements.txt" || warn "some ComfyUI requirements failed"
  [ -f "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt" ] && \
    "$pt" -m pip install -q -r "$COMFY/custom_nodes/ComfyUI-GGUF/requirements.txt"
  say "done. launch.sh runs it with --cache-none --reserve-vram 16 and the two"
  say "ROCm env vars; see the comment block there before changing any of them."
}

# ==========================================================
#  weights
# ==========================================================
hf_cli() {
  local h="$VENVS/tools/bin/hf"
  [ -x "$h" ] && { printf '%s' "$h"; return 0; }
  have hf && { command -v hf; return 0; }
  return 1
}

# fetch_weight <dir> <filename>
fetch_weight() {
  local dir="$1" name="$2" r repo path mb stage dest hf
  dest="$COMFY/models/$dir/$name"
  [ -f "$dest" ] && { say "have  $dir/$name"; return 0; }
  r="$(weight_row "$name")"
  if [ -z "$r" ]; then
    warn "$dir/$name - no source known."
    warn "  Either put the file in ComfyUI/models/$dir/ by hand, or add a row"
    warn "  to WEIGHTS in this file. Guessing a 20GB download is worse."
    return 1
  fi
  repo="$(printf '%s' "$r" | awk -F'|' '{print $3}')"
  path="$(printf '%s' "$r" | awk -F'|' '{print $4}')"
  mb="$(printf '%s' "$r" | awk -F'|' '{print $5}')"
  hf="$(hf_cli)" || { fail "no hf CLI - ./setup.sh venvs installs it"; return 1; }
  stage="$COMFY/models/_staging"
  mkdir -p "$stage" "$COMFY/models/$dir" || return 1
  say "get   $dir/$name  ($(gb "$mb"), $repo)"
  "$hf" download "$repo" --include "$path" --local-dir "$stage" >/dev/null || {
    fail "download failed: $repo $path"; return 1; }
  # The Hub keeps the repo's own layout under --local-dir; ComfyUI wants the
  # bare filename in the directory its loader reads. Same filesystem, so the
  # move is a rename and not a second 20GB write.
  mv -f "$stage/$path" "$dest" || { fail "could not move into $dir/"; return 1; }
  say "ok    $dir/$name"
}

do_weights() {
  launch_defaults
  local dir name todo=0 mb total=0 r
  while IFS='|' read -r dir name; do
    [ -n "${name:-}" ] || continue
    [ -f "$COMFY/models/$dir/$name" ] && continue
    r="$(weight_row "$name")"
    mb="$(printf '%s' "$r" | awk -F'|' '{print $5+0}')"
    total=$((total + mb)); todo=$((todo + 1))
  done < <(wanted_weights)
  if [ "$todo" = "0" ]; then say "every weight launch.sh and the workflow name is already on disk"; return 0; fi
  say "$todo file(s) missing, about $(gb "$total")."
  say "Free here: $(df -h "$HERE" | awk 'NR==2{print $4}')"
  ask "Download now?" || return 0
  local rc=0
  while IFS='|' read -r dir name; do
    [ -n "${name:-}" ] || continue
    fetch_weight "$dir" "$name" || rc=1
  done < <(wanted_weights)
  rm -rf "$COMFY/models/_staging"
  return $rc
}

# ==========================================================
#  models/ - the small weights and the CosyVoice checkout
# ==========================================================
do_models() {
  local hf; hf="$(hf_cli)" || { fail "no hf CLI - ./setup.sh venvs first"; return 1; }
  if [ -d "$MODELS/cosyvoice2" ]; then
    say "models/cosyvoice2 already here"
  else
    say "$M_TTS -> models/cosyvoice2 (about 4.6GB)"
    ask "Download now?" && { "$hf" download "$M_TTS" --local-dir "$MODELS/cosyvoice2" >/dev/null \
      || warn "download failed"; }
  fi
  # Not on PyPI. tts_server puts this checkout on sys.path itself, which is
  # why launch.sh checks for the directory and not for an import.
  if [ -d "$HERE/third_party/CosyVoice/cosyvoice" ]; then
    say "third_party/CosyVoice already here"
  else
    say "cloning CosyVoice (not a pip package)"
    mkdir -p "$HERE/third_party"
    git clone --recursive "$COSYVOICE_REPO" "$HERE/third_party/CosyVoice" \
      || warn "clone failed - tts will stay down"
  fi
  if have_venv tools && [ -f "$HERE/scripts/cosyvoice-req.txt" ]; then
    local pt; pt="$(venv_py tools)"
    if "$pt" -c 'import hyperpyyaml, whisper, onnxruntime, diffusers, conformer, wetext' >/dev/null 2>&1; then
      say "CosyVoice inference deps already installed"
    else
      # Its own requirements.txt pins grpcio 1.57 and torch 2.3.1, which would
      # build from source and clobber the torch installed above. The curated
      # list plus the constraints file is what avoids that.
      say "installing CosyVoice inference requirements"
      PIP_CONSTRAINT="$HERE/scripts/pip-constraints.txt" \
        "$pt" -m pip install -q -U -r "$HERE/scripts/cosyvoice-req.txt" \
        || warn "some CosyVoice deps failed - tts may not start"
    fi
  fi
  if [ -d "$MODELS/qwen3-embedding" ]; then
    say "models/qwen3-embedding already here"
  else
    say "$M_EMBED -> models/qwen3-embedding (about 1.2GB, optional:"
    say "no console page calls the embed slot yet)"
    ask "Download now?" && { "$hf" download "$M_EMBED" --local-dir "$MODELS/qwen3-embedding" >/dev/null \
      && "$(venv_py tools)" -m pip install -q -U sentence-transformers \
      || warn "embeddings not installed"; }
  fi
  local w
  for w in "$HERE"/voices/*.wav; do
    [ -f "$w" ] && [ -f "${w%.wav}.txt" ] && return 0
  done
  warn "voices/ has no name.wav + name.txt pair. CosyVoice2 has no speaker"
  warn "table - every synthesis is zero-shot off a reference clip and its"
  warn "exact transcript, so tts starts and then fails every request."
}

# ==========================================================
#  ollama
# ==========================================================
do_ollama() {
  launch_defaults
  have ollama || { fail "ollama not installed - https://ollama.com/download"; return 1; }
  # pull talks to the daemon. Start it the way launch.sh does, so the pid file
  # matches and ./launch.sh stop still owns it.
  if ! (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null; then
    say "starting ollama"
    mkdir -p "$LOGS"
    setsid ollama serve >"$LOGS/ollama.log" 2>&1 </dev/null &
    echo $! >"$LOGS/ollama.pid"
    local i=0
    while [ $i -lt 60 ] && ! (exec 3<>/dev/tcp/127.0.0.1/11434) 2>/dev/null; do sleep 0.5; i=$((i + 1)); done
  else
    exec 3>&-
  fi
  local m
  for m in "$LLM_MODEL" "$VERIFY_MODEL"; do
    [ -n "$m" ] || continue
    if ollama_has "$m"; then say "$m already pulled"; continue; fi
    say "ollama pull $m"
    ollama pull "$m" || warn "pull failed: $m"
  done
  say "The 27B writes the story; $VERIFY_MODEL is the one that checks every"
  say "render before it is published. Without it, takes land with no verdict."
}

# ==========================================================
#  ffmpeg
# ==========================================================
# No system ffmpeg on this box and no sudo to get one, so venvs/tools carries a
# static build (pip install imageio-ffmpeg) and bin/ffmpeg points at it.
# launch.sh puts bin/ first on PATH; the narration mux, the concat in Pipeline
# Runner and funasr's import all want it.
do_ffmpeg() {
  if [ -x "$HERE/bin/ffmpeg" ]; then say "bin/ffmpeg already points somewhere real"; return 0; fi
  have_venv tools || { fail "venvs/tools missing - ./setup.sh venvs first"; return 1; }
  local exe
  exe="$("$(venv_py tools)" -c 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null)"
  if [ -z "$exe" ] || [ ! -x "$exe" ]; then
    if have ffmpeg; then exe="$(command -v ffmpeg)"
    else fail "no ffmpeg: $(venv_py tools) -m pip install imageio-ffmpeg"; return 1; fi
  fi
  mkdir -p "$HERE/bin"
  ln -sfn "$exe" "$HERE/bin/ffmpeg" && say "bin/ffmpeg -> $exe"
}

# ==========================================================
#  all
# ==========================================================
do_all() {
  do_venvs  || return 1
  do_ffmpeg || true
  do_torch  || return 1
  do_comfy  || return 1
  do_weights || warn "some weights are still missing"
  do_models  || true
  do_ollama  || true
  printf '\n'
  do_check
}

usage() {
  printf '  usage: %s [check|all|venvs|torch|comfy|weights|models|ollama|ffmpeg]\n' "$0"
}

case "${1:-check}" in
  check|"") do_check ;;
  all)      do_all ;;
  venvs)    do_venvs ;;
  torch)    do_torch ;;
  comfy)    do_comfy ;;
  weights)  do_weights ;;
  models)   do_models ;;
  ollama)   do_ollama ;;
  ffmpeg)   do_ffmpeg ;;
  *)        usage; exit 2 ;;
esac
