#!/bin/bash
# ==========================================================
#  Bookreel - novel to video, macOS deployment
#  Apple Silicon with unified memory (M3 Max 64GB baseline).
#
#    Slot A  text    Qwen3.8-27B Q4_K_M   ~17GB  :8000  (llama.cpp Metal)
#    Slot S  text    Qwen3-8B    Q4_K_M    ~5GB  :8001  (faster fallback)
#    Slot B  images  Flux.2 (ComfyUI/MPS) ~24GB  :7860
#    Slot C  video   Hailuo               n/a    :9000  (see note below)
#    Always on       embeddings / TTS / align    :8002 :9100 :9101
#
#  The 4090 build makes slots take turns because 24GB of VRAM only fits one
#  at a time. Here the GPU shares the machine's 64GB, so A and B coexist with
#  room to spare - set EXCLUSIVE_SLOTS=1 below to get the old kill-first
#  behaviour back (worth doing on a 16-24GB Mac).
#
#  Slot C has no local Apple Silicon path: Hailuo's weights want CUDA kernels
#  that do not exist for Metal. On a Mac, point the console's Hailuo endpoint
#  at the hosted MiniMax API, or render video on the Windows box.
#
#  Double click in Finder, or:  ./setup.command
#  ASCII only, LF endings - same rule as setup.bat, kept here so the two
#  read side by side. The Chinese notes live in the macOS deployment txt.
#
#  Everything lives in this one file. It re-invokes itself with a command
#  word to run a slot in the background, e.g.
#      ./setup.command llm      ./setup.command comfy     ./setup.command health
#  One helper stays outside because it unzips a GitHub release:
#      scripts/get-llama-mac.sh
# ==========================================================

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SELF="$ROOT/$(basename "${BASH_SOURCE[0]}")"

# ==========================================================
#  Configuration - edit here
# ==========================================================
SCRIPTS="$ROOT/scripts"
MODELS="$ROOT/models"
VENVS="$ROOT/venvs"
LOGS="$ROOT/logs"
COMFY="$ROOT/ComfyUI"
HAILUO_DIR="$ROOT/hailuo"
LLAMA="$ROOT/llama"
SERVICES="$ROOT/services"
export HF_HOME="$MODELS/hf"

# --- model repos ----------------------------------------------------
M_MAIN_GGUF="unsloth/Qwen3.8-27B-GGUF"
F_MAIN_GGUF="Qwen3.8-27B-UD-Q4_K_M.gguf"
M_SMALL_GGUF="unsloth/Qwen3-8B-GGUF"
F_SMALL_GGUF="Qwen3-8B-Q4_K_M.gguf"
M_EMBED="Qwen/Qwen3-Embedding-0.6B"
M_FLUX="black-forest-labs/FLUX.2-dev"
M_TTS="FunAudioLLM/CosyVoice2-0.5B"

# --- ports ----------------------------------------------------------
# Slot S gets its own port here, unlike the Windows build where it reuses
# :8000. There is memory for both, and :8001 is what the web console already
# has in its "small model" field.
LLM_PORT=8000
SMALL_PORT=8001
EMBED_PORT=8002
COMFY_PORT=7860
HAILUO_PORT=9000
TTS_PORT=9100
ASR_PORT=9101

# --- 64GB unified tunables ------------------------------------------
# Q4_K_M weights are 16.4GB. KV cache is about 260KB per token at 63 layers,
# so 32K context costs roughly 8GB - comfortable here, where the 24GB build
# had to stop at 16K. 64K works too if you want whole chapters in one pass;
# add the q8_0 cache flags below to halve what it costs.
MAIN_CTX=32768
SMALL_CTX=32768

# Halves KV cache memory. Set to: --cache-type-k q8_0 --cache-type-v q8_0
KV_FLAGS=""

# Extra ComfyUI flags. On Metal do NOT pass --fp8_e4m3fn-unet: MPS has no fp8
# kernels, so torch upcasts every layer anyway and you pay the conversion for
# nothing. If an image OOMs, try --lowvram or --use-split-cross-attention.
COMFY_EXTRA=""

# Where each always-on service runs. Embeddings are a clean win on Metal.
# CosyVoice2 and FunASR both reach for ops Metal has no kernel for, so they
# fall back to CPU op by op and end up slower than just staying on CPU.
EMBED_DEVICE="mps"
TTS_DEVICE="cpu"
ASR_DEVICE="cpu"

# 1 = kill the other heavy slot before starting one (the 24GB behaviour).
EXCLUSIVE_SLOTS=0

# macOS caps what the GPU may wire down at about 3/4 of RAM. On 64GB that is
# roughly 48GB, which Flux.2 at 1080x1920 can bump into. Menu option M raises
# it for the current boot (needs sudo, resets on reboot).
WIRED_LIMIT_MB=57344

VIDEO_RES="720x1280"

# Metal has no kernel for a handful of ops; without this torch raises instead
# of quietly running those on the CPU.
export PYTORCH_ENABLE_MPS_FALLBACK=1

PY=""            # resolved by need_python

# ==========================================================
#  Small helpers
# ==========================================================
say()   { printf '  %s\n' "$*"; }
blank() { printf '\n'; }
pause() { printf '\n  '; read -r -p "press return " _ ; }

# 0 when something is listening on the port
busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }

need_python() {
  local c
  for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
  done
  [ -n "$PY" ] || { say "x no python3 found. Install it: brew install python@3.12"; return 1; }
  local v
  v="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$v" in
    3.11|3.12) : ;;
    *) say "! python $v - torch and ComfyUI wheels lag a release behind."
       say "  3.11 or 3.12 is the safe pick: brew install python@3.12" ;;
  esac
  return 0
}

# venv_py <name> -> echoes the interpreter inside venvs/<name>
venv_py() { printf '%s/%s/bin/python' "$VENVS" "$1"; }

have_venv() { [ -x "$(venv_py "$1")" ]; }

# spawn <name> <command-word> - run a slot in the background, log to logs/
# The command words all end in exec, so the pid we record is the real server
# process, not a wrapper shell - that is what makes kill_named simple.
spawn() {
  local name="$1" word="$2"
  mkdir -p "$LOGS"
  nohup bash "$SELF" "$word" >"$LOGS/$name.log" 2>&1 &
  printf '%s\n' "$!" > "$LOGS/$name.pid"
  say "+  $name starting -- tail -f logs/$name.log"
}

# kill_named <name> - stop what spawn started
kill_named() {
  local name="$1" pid
  [ -f "$LOGS/$name.pid" ] || return 0
  pid="$(cat "$LOGS/$name.pid")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    # Do not kill the process group: this menu shares it. Take the recorded
    # pid and anything it forked (ComfyUI spawns workers).
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    sleep 1
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" 2>/dev/null
  fi
  rm -f "$LOGS/$name.pid"
}

# ==========================================================
#  Command-word dispatch (used by the background slots)
# ==========================================================
if [ "$#" -gt 0 ]; then
  case "$1" in
    install)  do_word=do_install ;;
    hflogin)  do_word=do_hflogin ;;
    download) do_word=do_download ;;
    services) do_word=do_services ;;
    llm)      do_word=do_llm ;;
    small)    do_word=do_small ;;
    comfy)    do_word=do_comfy ;;
    hailuo)   do_word=do_hailuo ;;
    embed)    do_word=do_embed ;;
    tts)      do_word=do_tts ;;
    asr)      do_word=do_asr ;;
    health)   do_word=do_health ;;
    stop)     do_word=do_stop ;;
    getllama) do_word=do_getllama ;;
    *)
      say "unknown command: $1"
      say "try: install hflogin download services llm small comfy hailuo"
      say "     embed tts asr health stop getllama"
      exit 1 ;;
  esac
else
  do_word=menu
fi

# ==========================================================
#  1 - Install
# ==========================================================
do_install() {
  blank
  say "[check] toolchain"
  [ "$(uname -s)" = "Darwin" ] || { say "x this script is for macOS. On Windows run setup.bat."; return 1; }
  if [ "$(uname -m)" != "arm64" ]; then
    say "! arch is $(uname -m), not arm64. Under Rosetta the Metal paths are"
    say "  unavailable and torch installs a CPU build. Reopen Terminal with"
    say "  'Open using Rosetta' unchecked."
  fi
  xcode-select -p >/dev/null 2>&1 || {
    say "x Xcode command line tools missing. Run: xcode-select --install"; return 1; }
  command -v git >/dev/null 2>&1 || { say "x git not found"; return 1; }
  need_python || return 1
  say "python $("$PY" -c 'import sys;print(sys.version.split()[0])')  ($PY)"
  say "memory $(( $(sysctl -n hw.memsize) / 1073741824 ))GB unified, $(sysctl -n hw.model)"
  command -v ffmpeg >/dev/null 2>&1 || say "! ffmpeg missing (needed for step 05): brew install ffmpeg"

  mkdir -p "$MODELS" "$LOGS" "$VENVS"

  blank
  say "[1/4] llama.cpp (Metal) + Hugging Face CLI"
  have_venv llm || "$PY" -m venv "$VENVS/llm"
  "$(venv_py llm)" -m pip install -qU pip
  "$(venv_py llm)" -m pip install -qU "huggingface_hub[cli]" || say "! hf cli install failed"
  do_getllama || say "! llama.cpp download failed - slot A will not start"

  blank
  say "[2/4] venv-audio  (CosyVoice2 / FunASR)"
  have_venv audio || "$PY" -m venv "$VENVS/audio"
  "$(venv_py audio)" -m pip install -qU pip
  # On macOS the plain PyPI wheel IS the Metal-capable build - there is no
  # separate index to point at the way the CUDA side needs one.
  "$(venv_py audio)" -m pip install -qU torch torchaudio
  "$(venv_py audio)" -m pip install -qU funasr modelscope fastapi uvicorn soundfile python-multipart
  cosyvoice audio
  check_mps audio

  blank
  say "[3/4] venv-tools  (ffmpeg helpers / embeddings)"
  have_venv tools || "$PY" -m venv "$VENVS/tools"
  "$(venv_py tools)" -m pip install -qU pip
  "$(venv_py tools)" -m pip install -qU ffmpeg-python pillow requests sentence-transformers fastapi uvicorn
  check_mps tools

  blank
  say "[4/4] ComfyUI"
  [ -d "$COMFY" ] || git clone https://github.com/comfyanonymous/ComfyUI "$COMFY"
  have_venv comfy || "$PY" -m venv "$VENVS/comfy"
  "$(venv_py comfy)" -m pip install -qU pip
  "$(venv_py comfy)" -m pip install -qr "$COMFY/requirements.txt"
  # ComfyUI imports torchaudio from comfy/ldm/lightricks/vae/audio_vae.py and
  # its requirements.txt does not always pull it in.
  "$(venv_py comfy)" -m pip install -qU torch torchvision torchaudio
  check_mps comfy || say "! slot B will run on CPU and be unusably slow"

  blank
  say "Install done. Next: menu L (Hugging Face login), then 2 (download)."
  pause
}

# check_mps <venv> - torch present and Metal actually reachable
check_mps() {
  local p; p="$(venv_py "$1")"
  if "$p" -c 'import torch,sys; sys.exit(0 if torch.backends.mps.is_available() else 1)' 2>/dev/null; then
    say ".  venv-$1: torch $("$p" -c 'import torch;print(torch.__version__)'), Metal OK"
    return 0
  fi
  say "!  venv-$1: torch cannot see Metal."
  say "   Usually means an x86 python under Rosetta, or a CPU-only wheel."
  say "   Check with: $p -c 'import platform;print(platform.machine())'  -> arm64"
  return 1
}

# ==========================================================
#  L - Hugging Face login
# ==========================================================
do_hflogin() {
  local hf="$VENVS/llm/bin/hf"
  [ -x "$hf" ] || { say "x hf CLI missing - run menu 1 first"; pause; return 1; }
  blank
  say "Hugging Face access"
  say "--------------------------------------------------------"
  if ! "$hf" auth whoami >/dev/null 2>&1; then
    say "Not logged in yet."
    blank
    say "1. Create a READ token:  https://huggingface.co/settings/tokens"
    say "2. Paste it below (it will not echo)."
    blank
    local tok=""
    read -r -s -p "  Token: " tok; blank
    [ -n "$tok" ] || { say "No token entered."; pause; return 1; }
    "$hf" auth login --token "$tok" --add-to-git-credential || { say "x login failed"; pause; return 1; }
    tok=""
  fi
  "$hf" auth whoami
  blank
  say "Gated repos - open each page ONCE in a browser and click Accept:"
  say "  https://huggingface.co/black-forest-labs/FLUX.2-dev"
  blank
  say "Approval is usually instant. Without it the download returns"
  say '"Access denied. This repository requires approval."'
  blank
  local ans=""
  read -r -p "  Open the FLUX.2 page now? [y/N]: " ans
  case "$ans" in y|Y) open "https://huggingface.co/black-forest-labs/FLUX.2-dev" ;; esac
  pause
}

# ==========================================================
#  2 - Download weights
# ==========================================================
do_download() {
  local hf="$VENVS/llm/bin/hf"
  [ -x "$hf" ] || { say "x hf CLI missing - run menu 1 first"; pause; return 1; }
  blank
  say "Cache dir: $HF_HOME"
  say "64GB unified leaves room the 24GB build did not have: Q5_K_M or even"
  say "Q6_K for the 27B are fine here. Edit F_MAIN_GGUF above to switch."
  blank
  "$hf" auth whoami >/dev/null 2>&1 || {
    say "! Not logged in to Hugging Face - gated repos will fail."
    say "  Run menu option L first, then come back."
    pause; }

  say "[1/5] $M_MAIN_GGUF / $F_MAIN_GGUF   ~16GB"
  "$hf" download "$M_MAIN_GGUF" --include "$F_MAIN_GGUF" --local-dir "$MODELS/qwen3.8-27b-gguf" || {
    say "x download failed. Other quants in the same repo:"
    say "    Qwen3.8-27B-UD-Q5_K_M.gguf   19.8GB  better quality, fits easily here"
    say "    Qwen3.8-27B-UD-Q6_K.gguf     23GB    near-lossless, still fine on 64GB"; }

  say "[2/5] $M_SMALL_GGUF / $F_SMALL_GGUF   ~5GB"
  "$hf" download "$M_SMALL_GGUF" --include "$F_SMALL_GGUF" --local-dir "$MODELS/qwen3-8b-gguf" || say "! skipped"

  say "[3/5] $M_EMBED"
  "$hf" download "$M_EMBED" --local-dir "$MODELS/qwen3-embedding" || say "! skipped"

  say "[4/5] $M_FLUX   ~24GB   (GATED)"
  "$hf" download "$M_FLUX" --local-dir "$MODELS/flux2" || {
    say "x Access denied - this repo requires approval."
    say "  Open https://huggingface.co/black-forest-labs/FLUX.2-dev and click Accept,"
    say "  then run this step again. Alternative ungated checkpoints:"
    say "    Comfy-Org/flux2-dev-ComfyUI       repackaged, drops into ComfyUI"
    say "    Kijai/flux2-fp8                   fp8 single file, smallest download"; }

  say "[5/5] $M_TTS"
  "$hf" download "$M_TTS" --local-dir "$MODELS/cosyvoice2" || say "! skipped"

  blank
  say "FunASR pulls its weights on first call - nothing to predownload."
  say "Copy FLUX.2 files into $COMFY/models/{diffusion_models,text_encoders,vae}"
  say "On Metal prefer the bf16 unet over fp8: MPS has no fp8 kernels, so an"
  say "fp8 checkpoint is upcast on load and you gain nothing but the download."
  pause
}

# ==========================================================
#  3 - always-on services
# ==========================================================
do_services() {
  ensure_audio
  ensure_tools
  blank
  say "Always-on services: embeddings $EMBED_PORT, TTS $TTS_PORT, align $ASR_PORT"
  say "Anything already listening is left alone."
  blank
  svc "$EMBED_PORT" embed embed_server.py
  svc "$TTS_PORT"   tts   tts_server.py
  svc "$ASR_PORT"   asr   asr_server.py
  blank
}

# svc <port> <command-word> <script filename>
svc() {
  if [ ! -f "$SERVICES/$3" ]; then say "!  services/$3 missing - skipped"; return; fi
  if busy "$1"; then say ".  $2   already listening on $1 - left alone"; return; fi
  spawn "$2" "$2"
}

do_embed() {
  exec "$(venv_py tools)" "$SERVICES/embed_server.py" \
    --port "$EMBED_PORT" --model "$MODELS/qwen3-embedding" --device "$EMBED_DEVICE"
}

do_tts() {
  exec "$(venv_py audio)" "$SERVICES/tts_server.py" \
    --model "$MODELS/cosyvoice2" --port "$TTS_PORT" --device "$TTS_DEVICE"
}

do_asr() {
  exec "$(venv_py audio)" "$SERVICES/asr_server.py" \
    --port "$ASR_PORT" --device "$ASR_DEVICE"
}

# ==========================================================
#  A / S - text slots (llama.cpp on Metal)
# ==========================================================
do_llm() {
  local gguf="$MODELS/qwen3.8-27b-gguf/$F_MAIN_GGUF"
  [ -x "$LLAMA/llama-server" ] || do_getllama
  [ -x "$LLAMA/llama-server" ] || { say "x $LLAMA/llama-server not found. Run menu 1."; return 1; }
  [ -f "$gguf" ] || { say "x $gguf not found. Run menu 2."; return 1; }
  say "Slot A - text.  $F_MAIN_GGUF, ctx $MAIN_CTX, port $LLM_PORT"
  # -ngl 99 offloads every layer to Metal; the macOS build has no other
  # backend to fall back to, so this is just "use the GPU".
  exec "$LLAMA/llama-server" -m "$gguf" --host 127.0.0.1 --port "$LLM_PORT" \
    --alias qwen3.8-27b -ngl 99 -c "$MAIN_CTX" --parallel 1 --jinja --no-warmup $KV_FLAGS
}

do_small() {
  local gguf="$MODELS/qwen3-8b-gguf/$F_SMALL_GGUF"
  [ -x "$LLAMA/llama-server" ] || do_getllama
  [ -x "$LLAMA/llama-server" ] || { say "x $LLAMA/llama-server not found. Run menu 1."; return 1; }
  [ -f "$gguf" ] || { say "x $gguf not found. Run menu 2."; return 1; }
  say "Slot S - Qwen3-8B on port $SMALL_PORT (faster, weaker)."
  say "Its own port, so it can sit beside the 27B - that is what the console's"
  say "small-model field already points at."
  exec "$LLAMA/llama-server" -m "$gguf" --host 127.0.0.1 --port "$SMALL_PORT" \
    --alias qwen3-8b -ngl 99 -c "$SMALL_CTX" --parallel 2 --jinja --no-warmup $KV_FLAGS
}

# ==========================================================
#  B - images (ComfyUI on MPS)
# ==========================================================
do_comfy() {
  local p; p="$(venv_py comfy)"
  [ -x "$p" ] || { say "x venv-comfy missing - run menu 1."; return 1; }
  if ! "$p" -c 'import torch,torchaudio,sys; sys.exit(0 if torch.backends.mps.is_available() else 1)' 2>/dev/null; then
    say "x this venv's torch cannot see Metal (or torchaudio is missing)."
    say "  $p -m pip install -U torch torchvision torchaudio"
    return 1
  fi
  say "Slot B - images.  Flux.2 on Metal, port $COMFY_PORT"
  say "Expect roughly 3-5x the wall clock of a 4090 per image; memory is the"
  say "part that stops being a problem here, not speed."
  exec "$p" "$COMFY/main.py" --port "$COMFY_PORT" --enable-cors-header "*" $COMFY_EXTRA
}

# ==========================================================
#  C - video - not a local Mac slot
# ==========================================================
do_hailuo() {
  say "Slot C - video.  $VIDEO_RES, port $HAILUO_PORT"
  if [ ! -f "$HAILUO_DIR/server.py" ]; then
    say "x $HAILUO_DIR/server.py not found."
    blank
    say "There is no local Apple Silicon build of this one: the video models"
    say "this pipeline targets ship CUDA-only kernels, and Metal has no"
    say "equivalent. Two ways forward on a Mac:"
    say "  1. Point the console's Hailuo endpoint at the hosted MiniMax API"
    say "     (same request shape the console already sends:"
    say "      POST /v1/video/generations, GET /v1/query/video_generation)."
    say "  2. Keep step 04 on the Windows/4090 box and bring the clips back"
    say "     for step 05 - the compositing side is CPU and ffmpeg only."
    return 1
  fi
  exec "$(venv_py llm)" "$HAILUO_DIR/server.py" --port "$HAILUO_PORT" \
    --cors --resolution "$VIDEO_RES"
}

# ==========================================================
#  H - health   F - stop   M - raise the GPU memory ceiling
# ==========================================================
do_health() {
  blank
  say "[health]"
  local probes="$LLM_PORT|main LLM|/v1/models
$SMALL_PORT|small LLM|/v1/models
$EMBED_PORT|embeddings|/health
$COMFY_PORT|ComfyUI|/system_stats
$HAILUO_PORT|Hailuo|/health
$TTS_PORT|CosyVoice|/health
$ASR_PORT|align|/health"
  local port label path code
  while IFS='|' read -r port label path; do
    [ -n "$port" ] || continue
    code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$port$path" 2>/dev/null)"
    printf '   :%-5s %-11s HTTP %s\n' "$port" "$label" "$code"
  done <<< "$probes"
  blank
  say "[memory]  $(( $(sysctl -n hw.memsize) / 1073741824 ))GB unified"
  # There is no nvidia-smi here: on unified memory the GPU's footprint is
  # just the process RSS, so report that instead of a VRAM figure.
  local pid name
  for name in llm small comfy hailuo embed tts asr; do
    [ -f "$LOGS/$name.pid" ] || continue
    pid="$(cat "$LOGS/$name.pid")"
    if kill -0 "$pid" 2>/dev/null; then
      printf '   %-7s pid %-7s %s MB resident\n' "$name" "$pid" \
        "$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.0f", $1/1024}')"
    fi
  done
  local wl; wl="$(sysctl -n iogpu.wired_limit_mb 2>/dev/null || echo 0)"
  if [ "${wl:-0}" -gt 0 ]; then
    say "[gpu]     wired limit raised to ${wl} MB this boot"
  else
    say "[gpu]     wired limit at the macOS default (~75% of RAM); menu M raises it"
  fi
  pause
}

do_stop() {
  # Free the big slots. The always-on services are small and stay up.
  kill_named llm
  kill_named small
  kill_named comfy
  kill_named hailuo
  pkill -f 'llama-server -m' 2>/dev/null
  pkill -f "$COMFY/main.py" 2>/dev/null
  sleep 1
}

do_wired() {
  blank
  say "macOS lets the GPU wire down about 75% of RAM by default. Raising it to"
  say "${WIRED_LIMIT_MB} MB gives Flux.2 headroom at 1080x1920. It costs nothing"
  say "while unused, needs sudo, and resets on reboot."
  blank
  sudo sysctl -w iogpu.wired_limit_mb="$WIRED_LIMIT_MB" && say "done."
  pause
}

# ==========================================================
#  Helpers
# ==========================================================
do_getllama() {
  bash "$SCRIPTS/get-llama-mac.sh" "$LLAMA"
}

# Dependency guards. A venv built by an older run will not have packages
# added later, so check before use and install only what is missing.
ensure_audio() {
  have_venv audio || return 0
  local p; p="$(venv_py audio)"
  if ! "$p" -c 'import fastapi, uvicorn, soundfile, funasr' >/dev/null 2>&1 \
     || ! { "$p" -c 'import multipart' >/dev/null 2>&1 || "$p" -c 'import python_multipart' >/dev/null 2>&1; }; then
    say "[deps] venv-audio is missing packages - installing ..."
    "$p" -m pip install -qU fastapi uvicorn soundfile python-multipart funasr modelscope \
      || say "! some venv-audio packages failed to install"
  fi
  cosyvoice audio
}

ensure_tools() {
  have_venv tools || return 0
  local p; p="$(venv_py tools)"
  "$p" -c 'import fastapi, uvicorn, sentence_transformers' >/dev/null 2>&1 && return 0
  say "[deps] venv-tools is missing packages - installing ..."
  "$p" -m pip install -qU fastapi uvicorn sentence-transformers pillow requests ffmpeg-python \
    || say "! some venv-tools packages failed to install"
}

# CosyVoice2 is not on PyPI - clone it, then install a curated dependency
# list. Its own requirements.txt pins grpcio 1.57 and torch 2.3.1, which
# would build from source and clobber the torch we just installed.
cosyvoice() {
  local p; p="$(venv_py "$1")"
  if [ ! -d "$ROOT/third_party/CosyVoice/cosyvoice" ]; then
    say "[deps] CosyVoice is not cloned yet (it is not on PyPI) - fetching ..."
    mkdir -p "$ROOT/third_party"
    git clone --recursive https://github.com/FunAudioLLM/CosyVoice "$ROOT/third_party/CosyVoice" || {
      say "! CosyVoice clone failed - check git and network. TTS will stay offline."
      return 0; }
  elif "$p" -c 'import hyperpyyaml, whisper, onnxruntime, diffusers, conformer, wetext' >/dev/null 2>&1; then
    return 0
  fi
  say "[deps] installing CosyVoice inference requirements ..."
  PIP_CONSTRAINT="$SCRIPTS/pip-constraints.txt" \
    "$p" -m pip install -qU -r "$SCRIPTS/cosyvoice-req.txt" \
    || say "! some CosyVoice deps failed - TTS may not start"
}

# ==========================================================
#  Menu
# ==========================================================
slot() {           # slot <name> <command-word> <heavy?>
  if [ "$3" = "heavy" ] && [ "$EXCLUSIVE_SLOTS" = "1" ]; then do_stop; fi
  spawn "$1" "$2"
}

menu() {
  while true; do
    clear
    blank
    printf '   Bookreel - novel to video   [Apple Silicon, %sGB unified]\n' \
      "$(( $(sysctl -n hw.memsize) / 1073741824 ))"
    say "--------------------------------------------------------"
    say " 1   Install                 venvs + llama.cpp + ComfyUI"
    say " L   Log in to Hugging Face  needed for gated repos"
    say " 2   Download model weights"
    say " 3   Start always-on svcs    embeddings / TTS / align"
    blank
    say " A   Slot A - text     Qwen3.8-27B   script, shots, prompts   :$LLM_PORT"
    say " S   Slot S - small    Qwen3-8B      faster fallback          :$SMALL_PORT"
    say " B   Slot B - images   Flux.2        character and scene plates"
    say " C   Slot C - video    Hailuo        no local Mac build - see notes"
    blank
    say " H   Health check      F   Free the GPU      T   Tail a log"
    say " M   Raise GPU memory ceiling            W   Open web console"
    say " Q   Quit"
    say "--------------------------------------------------------"
    blank
    local ch=""
    read -r -p "  Choose: " ch
    case "$ch" in
      1)   do_install ;;
      l|L) do_hflogin ;;
      2)   do_download ;;
      3)   do_services; pause ;;
      a|A) slot llm    llm    heavy; pause ;;
      s|S) slot small  small  light; pause ;;
      b|B) slot comfy  comfy  heavy; pause ;;
      c|C) slot hailuo hailuo heavy; sleep 1; tail -n 20 "$LOGS/hailuo.log" 2>/dev/null; pause ;;
      h|H) do_health ;;
      f|F) do_stop; say "GPU freed."; pause ;;
      t|T) tail_log ;;
      m|M) do_wired ;;
      w|W) open "$ROOT/Pipeline Runner.dc.html" ;;
      q|Q) exit 0 ;;
    esac
  done
}

tail_log() {
  blank
  local files name
  files="$(ls -1 "$LOGS"/*.log 2>/dev/null)"
  [ -n "$files" ] || { say "no logs yet"; pause; return; }
  printf '%s\n' "$files" | sed 's|.*/|   |'
  blank
  read -r -p "  Which one (name, no .log)? " name
  [ -f "$LOGS/$name.log" ] || { say "no such log"; pause; return; }
  say "ctrl-C to come back to the menu"
  trap ' ' INT
  tail -n 40 -f "$LOGS/$name.log"
  trap - INT
}

# ==========================================================
"$do_word"
