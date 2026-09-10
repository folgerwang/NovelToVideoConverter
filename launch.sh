#!/usr/bin/env bash
# Bookreel . bring the whole stack up on Linux after a fresh reboot.
#
# Windows has setup.bat and macOS has setup.command; neither runs here, and
# nothing in the repo started the Linux services, so every reboot meant
# remembering four command lines and the two flags that are easy to miss.
# This is that list, written down.
#
#     ./launch.sh            start everything that is not already up
#     ./launch.sh stop       stop what this script started
#     ./launch.sh status     what is listening, and what is not
#     ./launch.sh logs web   tail one log (web | story | comfy | ollama |
#                            embed | tts | asr | video)
#
# Starting is idempotent: a port that already answers is left alone, so
# running it twice is harmless and it is safe to re-run after a crash.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS="$HERE/logs"
VENVS="$HERE/venvs"
COMFY="$HERE/ComfyUI"

# --- ports (same numbers as setup.bat / setup.command) ---------------
LLM_PORT=11434          # ollama. The mac build runs llama.cpp on :8000
                        # instead; story_server speaks OpenAI either way.
STORY_PORT=8010
COMFY_PORT=7860
WEB_PORT=8080

# The optional slots. Each is skipped rather than failed when its weights are
# not here, so this list is the whole pipeline whether or not it is provisioned.
EMBED_PORT=8002         # retrieval
TTS_PORT=9100           # narration, step 3
ASR_PORT=9101           # alignment, step 3 - the wav plus its text -> an SRT
VIDEO_PORT=9000         # slot C, MiniMax-H3

# --- what to run -----------------------------------------------------
LLM_MODEL="${LLM_MODEL:-qwen3.8-27b}"

# Weights that are not ComfyUI's live under models/. Nothing in this script
# downloads anything; each optional slot names what it wants and steps aside
# until that is on disk.
MODELS="${MODELS:-$HERE/models}"

# No system ffmpeg on this box and no sudo to get one, so venvs/tools carries a
# static build (pip install imageio-ffmpeg) and bin/ffmpeg is a symlink to it.
# The narration mux and the concat script in Pipeline Runner both need it, and
# funasr warns on import without it.
[ -x "$HERE/bin/ffmpeg" ] && export PATH="$HERE/bin:$PATH"

# Slot C's weights, by filename inside ComfyUI/models. These are h3_server's
# own defaults, repeated here because start_video both checks for them and
# passes them - naming them once is what keeps those two from drifting apart.
H3_UNET="${H3_UNET:-minimax_h3_fl2va_pruned_fp8_scaled.safetensors}"
# 2026-09-07: the Heretic (uncensored) Qwen3-VL-32B encoder, same NVFP4 size as
# Comfy-Org's, a drop-in for CLIPLoader(type=minimax). sha256 a166c7bb… verified.
H3_CLIP="${H3_CLIP:-qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors}"
H3_VAE="${H3_VAE:-minimax_h3_video_vae_fp16.safetensors}"
# MiniMax-H3 renders picture and sound together; without this second VAE the
# soundtrack is generated and then discarded, and every clip comes out silent.
# Off by default. h3's audio branch does not produce ambience, it produces an
# invented speaker talking in no language, and every segment invents another
# one - four per 15 s shot, all under the Chinese narration. The clip's sound
# is CosyVoice now, so this only cost a VAE decode (~50 s a segment, ~3 min a
# shot) to make the mix worse. Set it back to the filename to get h3's audio
# again, and raise BOOKREEL_AMBIENT_GAIN above 0 so it reaches the mix.
H3_AUDIO_VAE="${H3_AUDIO_VAE-}"
# ${H3_LORA-...} not ${H3_LORA:-...}: H3_LORA="" must mean "no LoRA" (a UNet
# with the turbo already fused in must not have it applied twice), and with
# the colon form an empty value silently fell back to the default.
H3_LORA="${H3_LORA-minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors}"
# Sampling steps for slot C. Empty = h3_server's own rule (4 with a turbo LoRA,
# 20 without). A UNet with the turbo already fused in (no LoRA) wants 4 here.
H3_STEPS="${H3_STEPS:-}"
# Canvas ceiling for slot C, in pixels. h3's own limit is 768*1344 = 1032192
# and this now runs at it: 1312x736 for a 16:9 clip, 736x1312 for 9:16.
# Lower it to trade resolution for wall clock; everything is upscaled before
# the final cut, but an upscaler can invent pixels, not detail.
#
# Cost is frames x pixels, so this budget is really a choice between width and
# length. Measured 2026-09-08, same shot and first frame, one clip each:
#     56 frames 2.33s 1312x736  13:00 total,  8:06 sampling  -> 5.6 GPU-min/film-s
#     73 frames 3.04s 1312x736  17:52 total, 11:33 sampling  -> 5.9
#    107 frames 4.46s 1088x608  16:35 total, 10:44 sampling  -> 3.7
# At 661504 (1088x608) a 15 s shot cost ~3.7 GPU-min per second of film, about
# 56 min a shot; at the full budget it is ~5.9, about 88 min. That is the price
# of the sharper frame - set H3_MAX_PIXELS=661504 to go back to the faster,
# longer-per-hour setting without editing this file.
H3_MAX_PIXELS="${H3_MAX_PIXELS:-1032192}"

# BIND_LAN=0  everything listens on 127.0.0.1 - this machine only.
# BIND_LAN=1  the console AND the model services bind 0.0.0.0, so you can
#             drive the pipeline from a phone or another machine here. The
#             console's endpoint fields follow the page's own address, so
#             there is nothing to type on the other device.
#             There is no password on any of it and CORS is already "*", so
#             only do this on a network you trust.
# Defaults to 1 here (the mac build defaults to 0) because the console on
# this machine is normally driven from another device on the LAN.
BIND_LAN="${BIND_LAN:-1}"

# Extra ComfyUI flags. Every one of these is load-bearing on this box:
#
#   --reserve-vram 16   This machine is a 128GB Strix Halo: the BIOS gives the
#                       GPU 96GiB as real, dedicated VRAM and Linux the other
#                       31GiB (/sys/class/drm/card0/device/mem_info_vram_total
#                       says 96G; GTT is a separate 16G). An earlier version of
#                       this file reserved 72 on the belief that the 96 was a
#                       phantom. It is not, and 72 left ComfyUI a 24GiB budget
#                       - smaller than the Q4 UNet (18.8GB) plus the fp4 text
#                       encoder (11.7GB) together, so they could never both be
#                       resident: evict, re-stage through the 31GiB host side,
#                       swap fills, segfault in quant_ops. Reserving 16 leaves
#                       ~45GiB; measured 2026-09-05: both models "loaded
#                       completely", peak host RAM 16/31GiB, swap untouched.
#   --use-pytorch-cross-attention  torch's SDPA, which needs the aotriton flag
#                       below to have any kernels at all on this GPU. It
#                       replaced --use-split-cross-attention on 2026-09-07:
#                       split attention wanted 54.9GiB for one 3-second
#                       MiniMax-H3 clip (OOM, video impossible) and is also
#                       slower on images - measured on the same Flux.2 plate,
#                       49.0 s/step split vs 42.2 s/step SDPA.
#   --cache-none        Counter-intuitively the FAST setting here, and the
#                       reason is the 31GiB host side, not VRAM. ComfyUI keeps
#                       a host-RAM copy of every loaded model (the "offload
#                       device"). Text encoder 11.7GB + UNet 18.8GB = 30.5GB of
#                       host copies, which does not fit beside the OS in 31GiB.
#                       --cache-none releases the encoder after each prompt, so
#                       the UNet has room to load: ~400s per image including
#                       the reload. Everything tried on 2026-09-06 to keep both
#                       resident instead was slower, because each one spilled
#                       the host side into swap:
#                         --highvram                  -> ComfyUI 0.34 pinned
#                             23.5GB of host RAM and streamed weights per layer;
#                             1.5GB of VRAM in use, 25 min per image.
#                         --highvram --disable-pinned-memory --disable-async-offload
#                             --disable-dynamic-vram    -> host copies kept,
#                             500MB free, swap 4GB.
#                         --gpu-only + the three above -> UNet never "loaded
#                             completely", read from the mmap every step,
#                             >13 min per image.
#                         no --cache-none, no pinning  -> UNet loading into
#                             VRAM at ~10MB/s while the host swapped.
#                       The real fix is not a flag: rebalance the BIOS UMA
#                       carve-out from 96/31 to something like 64/63. With
#                       ~60GiB of host RAM both copies fit, --highvram works as
#                       intended, and the per-image reload disappears.
#
# COMFY_CACHE picks between those two worlds. --cache-none is right for step 2
# (Flux.2: two models, 30.5GB of host copies, one image per prompt). It is
# wrong for step 3, where one 105-clip batch reloads the same four MiniMax-H3
# models 105 times. Measured on C01S03, 2026-09-08: 12:55 for one clip, of
# which 8:06 was sampling, 2:09 the video VAE decode, 0:43 the audio VAE and
# mux, and ~1:57 the prologue - loading 40.6GB of weights plus the 32B text
# encoder pass. Dropping --cache-none is what keeps the four resident between
# clips; set COMFY_CACHE="" for that. The catch is host RAM, not VRAM: with
# the models loaded this process sits at ~24GiB RSS of the 31GiB Linux side
# (measured: 30GiB used mid-prompt, 8GiB the moment --cache-none freed them),
# so keeping them resident leaves almost nothing for tts and asr. Watch
# `free -g` swap after switching; if it climbs, put --cache-none back, and
# know that the real fix is the BIOS UMA split, 96/31 -> 64/63.
COMFY_CACHE="${COMFY_CACHE---cache-none}"
COMFY_EXTRA="${COMFY_EXTRA:---use-pytorch-cross-attention --reserve-vram 16 $COMFY_CACHE}"

PAGE_RUNNER="Pipeline%20Runner.dc.html"
PAGE_STUDIO="Story%20Studio.dc.html"

# ==========================================================
#  Small helpers
# ==========================================================
say()  { printf '  %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
fail() { printf '  x %s\n' "$*"; }

# An optional slot that stood down, with the one thing that was missing.
# skip <name> <port> <reason>
SKIPPED=0
skip() { printf '  %-8s :%-5s %s\n' "$1" "$2" "$3"; SKIPPED=1; }

# has_mod <python> <module>  - is it installed, without importing it. This runs
# on every launch and importing torch or funasr just to ask costs seconds.
has_mod() {
  "$1" -c "import importlib.util as u, sys; sys.exit(0 if u.find_spec('$2') else 1)" 2>/dev/null
}

bind_host() { if [ "$BIND_LAN" = "1" ]; then printf '0.0.0.0'; else printf '127.0.0.1'; fi; }

# 0 when something is listening. /dev/tcp keeps this working on a box with
# neither lsof nor ss installed, which a fresh minimal install often is.
busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- ; }

# The address to reach this machine at. connect() on a UDP socket sends
# nothing; it just picks the outbound route.
lan_ip() {
  python3 - <<'PYIP' 2>/dev/null
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("192.0.2.1", 9))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
PYIP
}

# wait_port <port> <seconds> <label>
wait_port() {
  local port="$1" limit="$2" label="$3" i=0
  while [ "$i" -lt "$((limit * 2))" ]; do
    busy "$port" && return 0
    sleep 0.5; i=$((i + 1))
  done
  fail "$label did not come up within ${limit}s - ./launch.sh logs $label"
  return 1
}

# spawn <name> <command...>  - detached, own process group, logged.
# setsid so the service outlives this shell and so stop can signal the whole
# group: ComfyUI forks workers that a bare kill on the parent leaves behind.
spawn() {
  local name="$1"; shift
  mkdir -p "$LOGS"
  setsid "$@" >"$LOGS/$name.log" 2>&1 </dev/null &
  echo $! >"$LOGS/$name.pid"
}

# ==========================================================
#  The services
# ==========================================================

# Step 1's LLM. story_server only speaks OpenAI over HTTP, so anything
# serving /v1 on this port works; ollama is what is installed here.
start_ollama() {
  if busy "$LLM_PORT"; then say "llm      :$LLM_PORT already up"; return 0; fi
  command -v ollama >/dev/null 2>&1 || { fail "ollama not installed - https://ollama.com/download"; return 1; }
  # ollama binds 127.0.0.1 unless told otherwise. It never needs to be
  # reachable from the LAN: only story_server talks to it, from this box.
  spawn ollama ollama serve
  wait_port "$LLM_PORT" 30 ollama || return 1
  say "llm      :$LLM_PORT ollama"
  if ! ollama list 2>/dev/null | awk '{print $1}' | grep -q "^${LLM_MODEL}\(:latest\)\?$"; then
    warn "model '$LLM_MODEL' is not pulled - ollama pull $LLM_MODEL"
    warn "  (or set LLM_MODEL to one of: $(ollama list 2>/dev/null | awk 'NR>1{printf "%s ", $1}'))"
  fi
}

# Step 1 of 3: blueprint, chapters, scripts, asset prompts. FastAPI, so it
# needs venv-tools; the console's Story Studio page is useless without it.
start_story() {
  if busy "$STORY_PORT"; then say "story    :$STORY_PORT already up"; return 0; fi
  local py="$VENVS/tools/bin/python"
  [ -x "$py" ] || { fail "venvs/tools missing - it is the venv holding fastapi"; return 1; }
  "$py" -c 'import fastapi, uvicorn' 2>/dev/null || { fail "venvs/tools has no fastapi: $py -m pip install fastapi uvicorn"; return 1; }
  spawn story "$py" "$HERE/services/story_server.py" \
    --host "$(bind_host)" --port "$STORY_PORT" \
    --llm "http://127.0.0.1:$LLM_PORT/v1" --model "$LLM_MODEL"
  wait_port "$STORY_PORT" 30 story || return 1
  say "story    :$STORY_PORT (llm $LLM_MODEL)"
}

# Step 2 of 3: images. Two flags matter and both are easy to lose:
#   --enable-cors-header  the console is served from :8080 and calls :7860,
#                         which is cross-origin. Without this ComfyUI installs
#                         its origin-only middleware and sends no
#                         Access-Control-Allow-Origin, so every generate ends
#                         as a bare "Failed to fetch" in the browser with
#                         nothing in ComfyUI's log to explain it.
#   --listen              bind wide, only when BIND_LAN says so.
start_comfy() {
  if busy "$COMFY_PORT"; then say "comfy    :$COMFY_PORT already up"; return 0; fi
  local py="$VENVS/comfy/bin/python"
  [ -x "$py" ] || { fail "venvs/comfy missing"; return 1; }
  [ -f "$COMFY/main.py" ] || { fail "ComfyUI not found at $COMFY"; return 1; }
  if ! "$py" -c 'import torch,sys; sys.exit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
    warn "this venv's torch cannot see the GPU - images will fall back to CPU and crawl"
  fi
  local listen=()
  [ "$BIND_LAN" = "1" ] && listen=(--listen 0.0.0.0)
  # cd: ComfyUI resolves models/, custom_nodes/ and output/ relative to cwd.
  # TORCH_BLAS_PREFER_HIPBLASLT=0: on gfx1151 hipBLASLt has no kernel for some
  # of MiniMax-H3's GEMM shapes ("getKernel failed ... named symbol not found",
  # then HIPBLAS_STATUS_INTERNAL_ERROR and a segfault in minimax/model.py).
  # rocBLAS has them all; it is a little slower on the shapes hipBLASLt does
  # have, and it does not crash.
  # TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1: torch's flash / memory-efficient
  # SDPA is marked experimental on this GPU and disabled by default; with it
  # on, attention over 131k tokens costs 0.5GiB (measured), without it the
  # split path needs 54GiB for a 3-second MiniMax-H3 clip and video is
  # impossible. Only matters with --use-pytorch-cross-attention.
  ( cd "$COMFY" && spawn comfy env TORCH_BLAS_PREFER_HIPBLASLT=0 TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1 "$py" main.py --port "$COMFY_PORT" \
      --enable-cors-header "*" "${listen[@]}" $COMFY_EXTRA )
  # Slower than the rest: it imports torch and scans every custom node.
  wait_port "$COMFY_PORT" 180 comfy || return 1
  say "comfy    :$COMFY_PORT"
}

# ---------------------------------------------------------------------
# The optional slots.
#
# All four lazy-load their heavy imports, so once started they always answer
# /health - reporting loaded:false until the first request pulls the model in,
# which is exactly what the console polls for. That is why the checks below
# are about weights on disk rather than about the service itself.
# ---------------------------------------------------------------------

# Retrieval. CPU by design: the GPU belongs to the LLM and to Flux, and
# Qwen3-Embedding-0.6B is small enough not to care. Worth knowing that no
# console page calls :8002 yet - this exists so the slot is there, not
# because step 1 or step 3 is waiting on it.
start_embed() {
  if busy "$EMBED_PORT"; then say "embed    :$EMBED_PORT already up"; return 0; fi
  local py="$VENVS/tools/bin/python"
  [ -x "$py" ] || { skip embed "$EMBED_PORT" "skipped - venvs/tools missing"; return 0; }
  [ -d "$MODELS/qwen3-embedding" ] || { skip embed "$EMBED_PORT" "skipped - no models/qwen3-embedding"; return 0; }
  has_mod "$py" sentence_transformers || { skip embed "$EMBED_PORT" "skipped - venvs/tools/bin/pip install sentence-transformers"; return 0; }
  spawn embed "$py" "$HERE/services/embed_server.py" \
    --host "$(bind_host)" --port "$EMBED_PORT" \
    --model "$MODELS/qwen3-embedding" --device cpu
  wait_port "$EMBED_PORT" 30 embed || return 1
  say "embed    :$EMBED_PORT (cpu)"
}

# Narration. CosyVoice2 has no speaker table - every synthesis is zero-shot off
# a reference clip, so a "voice" is a pair of files in voices/: name.wav and
# name.txt, its exact transcript. With no pair the service still starts and
# still passes /health, but every /tts fails; that is a worse outcome than
# saying so here, so an empty voices/ counts as a missing prerequisite.
start_tts() {
  if busy "$TTS_PORT"; then say "tts      :$TTS_PORT already up"; return 0; fi
  local py="$VENVS/tools/bin/python"
  [ -x "$py" ] || { skip tts "$TTS_PORT" "skipped - venvs/tools missing"; return 0; }
  [ -d "$MODELS/cosyvoice2" ] || { skip tts "$TTS_PORT" "skipped - no models/cosyvoice2"; return 0; }
  # Not a pip package. tts_server puts this checkout on sys.path itself.
  [ -d "$HERE/third_party/CosyVoice" ] || { skip tts "$TTS_PORT" "skipped - no third_party/CosyVoice checkout"; return 0; }
  local voice="" w
  for w in "$HERE"/voices/*.wav; do
    if [ -f "$w" ] && [ -f "${w%.wav}.txt" ]; then voice="$w"; break; fi
  done
  [ -n "$voice" ] || { skip tts "$TTS_PORT" "skipped - voices/ has no name.wav + name.txt pair"; return 0; }
  spawn tts "$py" "$HERE/services/tts_server.py" \
    --host "$(bind_host)" --port "$TTS_PORT" \
    --model "$MODELS/cosyvoice2" --voices "$HERE/voices" --device cpu
  wait_port "$TTS_PORT" 30 tts || return 1
  say "tts      :$TTS_PORT (cpu, voice $(basename "${voice%.wav}"))"
}

# Alignment. FunASR pulls fa-zh from ModelScope on the first /align rather than
# at startup, so this one needs a network connection once and nothing under
# models/ beforehand - which is why there is no weights check here.
start_asr() {
  if busy "$ASR_PORT"; then say "asr      :$ASR_PORT already up"; return 0; fi
  local py="$VENVS/tools/bin/python"
  [ -x "$py" ] || { skip asr "$ASR_PORT" "skipped - venvs/tools missing"; return 0; }
  has_mod "$py" funasr || { skip asr "$ASR_PORT" "skipped - venvs/tools/bin/pip install funasr"; return 0; }
  spawn asr "$py" "$HERE/services/asr_server.py" \
    --host "$(bind_host)" --port "$ASR_PORT" \
    --device cpu --cache "$MODELS/funasr"
  wait_port "$ASR_PORT" 30 asr || return 1
  say "asr      :$ASR_PORT (cpu, fa-zh on first call)"
}

# Slot C. h3_server has two backends and only one of them exists on Linux:
# --backend h3c is antirez's Metal program, Apple Silicon only. --backend comfy
# drives ComfyUI's own MiniMax-H3 nodes over :7860 and runs on any GPU ComfyUI
# runs on, which is this one. It needs no venv of its own and no models/ entry
# either: ComfyUI already holds the weights and does all of the work, so this
# process is only the queue and the hosted-API shape the console speaks.
start_video() {
  if busy "$VIDEO_PORT"; then say "video    :$VIDEO_PORT already up"; return 0; fi
  local py="$VENVS/tools/bin/python"
  [ -x "$py" ] || { skip video "$VIDEO_PORT" "skipped - venvs/tools missing"; return 0; }
  # All of them, not just the first: naming one at a time would mean a
  # re-run per missing file to find out what else is wanted.
  local m miss=""
  for m in "diffusion_models/$H3_UNET" "text_encoders/$H3_CLIP" "vae/$H3_VAE"; do
    [ -f "$COMFY/models/$m" ] || miss="$miss ${m##*/}"
  done
  [ -z "$miss" ] || { skip video "$VIDEO_PORT" "skipped - ComfyUI/models has no$miss"; return 0; }
  # The turbo LoRA is what makes 4 steps enough. Without it h3_server is right
  # to use 20, but that is five times the wait, so say so rather than let it
  # look like the render simply hung.
  local lora="$H3_LORA"
  if [ -n "$lora" ] && [ ! -f "$COMFY/models/loras/$lora" ]; then
    warn "no turbo LoRA in ComfyUI/models/loras - slot C falls back to 20 steps"
    lora=""
  fi
  busy "$COMFY_PORT" || warn "comfy :$COMFY_PORT is down - slot C will accept jobs and fail every one"
  spawn video "$py" "$HERE/services/h3_server.py" \
    --host "$(bind_host)" --port "$VIDEO_PORT" \
    --backend comfy --comfy "http://127.0.0.1:$COMFY_PORT" \
    --comfy-unet "$H3_UNET" --comfy-clip "$H3_CLIP" --comfy-vae "$H3_VAE" \
    --comfy-audio-vae "$H3_AUDIO_VAE" \
    --comfy-lora "$lora" --out "$HERE/output/video" ${H3_STEPS:+--steps "$H3_STEPS"} \
    ${H3_MAX_PIXELS:+--max-pixels "$H3_MAX_PIXELS"}
  wait_port "$VIDEO_PORT" 30 video || return 1
  say "video    :$VIDEO_PORT (comfy backend$([ -n "$lora" ] && printf ', turbo 4-step'))"
}

# The console itself. Stdlib only, no venv, and deliberately the last thing
# to start and the first thing that should ever come up.
start_web() {
  if busy "$WEB_PORT"; then say "web      :$WEB_PORT already up"; return 0; fi
  spawn web python3 "$HERE/services/web_server.py" \
    --host "$(bind_host)" --port "$WEB_PORT"
  wait_port "$WEB_PORT" 20 web || return 1
  say "web      :$WEB_PORT"
}

# ==========================================================
#  Commands
# ==========================================================
do_start() {
  printf '\n  Bookreel - starting\n\n'
  local rc=0
  start_ollama || rc=1
  start_story  || rc=1
  start_comfy  || rc=1
  # After comfy: slot C talks to it, and the rest are quick either way.
  start_embed  || rc=1
  start_tts    || rc=1
  start_asr    || rc=1
  start_video  || rc=1
  start_web    || rc=1

  local host="127.0.0.1" ip=""
  printf '\n  Console\n'
  say "local:  http://127.0.0.1:$WEB_PORT/$PAGE_RUNNER"
  say "        http://127.0.0.1:$WEB_PORT/$PAGE_STUDIO"
  if [ "$BIND_LAN" = "1" ]; then
    ip="$(lan_ip)"
    if [ -n "$ip" ]; then
      say "remote: http://$ip:$WEB_PORT/$PAGE_RUNNER   (same network)"
      say "Model services are bound to the LAN too - no password on any of them."
    fi
  else
    say "This machine only. Run with BIND_LAN=1 to reach it from another device."
  fi

  if [ "$SKIPPED" = "1" ]; then
    printf '\n'
    say "The skipped slots above are optional and nothing here downloads their"
    say "weights. The console reports each endpoint offline until you install"
    say "what its line names, then re-run ./launch.sh - starting is idempotent,"
    say "so what is already up is left alone."
  fi
  printf '\n'
  return $rc
}

do_stop() {
  printf '\n  Bookreel - stopping\n\n'
  local name pid
  for name in web video asr tts embed comfy story ollama; do
    pid="$(cat "$LOGS/$name.pid" 2>/dev/null)"
    if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
      # Negative pid = the whole process group, which is why spawn used
      # setsid: ComfyUI's workers do not die with their parent.
      kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
      say "stopped $name (pid $pid)"
      rm -f "$LOGS/$name.pid"
    else
      say "$name not running from this script"
      rm -f "$LOGS/$name.pid"
    fi
  done
  printf '\n'
}

do_status() {
  printf '\n  Bookreel - status\n\n'
  local row
  for row in "llm     $LLM_PORT" "story   $STORY_PORT" "comfy   $COMFY_PORT" \
             "embed   $EMBED_PORT" "tts     $TTS_PORT" "asr     $ASR_PORT" \
             "video   $VIDEO_PORT" "web     $WEB_PORT"; do
    set -- $row
    if busy "$2"; then printf '  %-8s :%-6s up\n' "$1" "$2"
    else                printf '  %-8s :%-6s down\n' "$1" "$2"; fi
  done
  printf '\n'
}

do_logs() {
  local name="${1:-}"
  [ -n "$name" ] || { ls -1 "$LOGS"/*.log 2>/dev/null | sed 's|.*/|  |' || say "no logs yet"; return; }
  [ -f "$LOGS/$name.log" ] || { fail "no log named $name"; return 1; }
  tail -n 40 -f "$LOGS/$name.log"
}

case "${1:-start}" in
  start|"")      do_start ;;
  stop)          do_stop ;;
  restart)       do_stop; do_start ;;
  status)        do_status ;;
  logs)          shift; do_logs "${1:-}" ;;
  *)             printf '  usage: %s [start|stop|restart|status|logs <name>]\n' "$0"; exit 2 ;;
esac
