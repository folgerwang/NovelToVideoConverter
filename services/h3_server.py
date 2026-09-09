# -*- coding: utf-8 -*-
"""Bookreel . local video service (slot C).

Speaks the HTTP shape the web console already uses for MiniMax's hosted API,
so slot C works locally with no console change. Two engines sit behind it:

    --backend h3c     antirez's h3.c, a native MiniMax-H3 engine for Metal.
                      Apple Silicon only -- it is a Metal program, there is no
                      CUDA/ROCm/CPU path.
    --backend comfy   ComfyUI's built-in MiniMax-H3 nodes over its /prompt API.
                      Runs anywhere ComfyUI runs, which is what makes slot C
                      possible on NVIDIA and AMD as well as Apple.

Both produce the same mp4 through the same endpoints; only the worker differs.
The request shaping is shared because it has to be: ComfyUI's own H3 nodes use
exactly the same mechanical limits as h3.c (canvas multiples of 32 under a
768*1344 pixel budget, frames on the 5+17n grid at 24fps), so _legal_canvas()
and _align_frames() are correct for both.

    POST /v1/video/generations           -> {"task_id": ...}
    GET  /v1/query/video_generation      -> {"status": "Success", "video_url": ...}
    GET  /v1/files/{file_id}             -> the mp4 itself

    python h3_server.py --bin ../third_party/h3.c/h3 --model ../models/minimax-h3
    python h3_server.py --backend comfy --comfy http://127.0.0.1:7860

Why a queue instead of just running the command: one render saturates the GPU
for tens of minutes, so a second concurrent job would only make both slower and
risk swapping. Jobs are accepted immediately, run one at a time, and the
console polls -- which is exactly what the hosted API does too.

Two things this shim silently fixes for the caller:

  * resolution. The console asks for 1080x1920 because that is what the cloud
    model does. h3.c has a hard canvas ceiling (768x1344, sides must be
    multiples of 32), so we scale the request down to the largest legal canvas
    with the same aspect ratio. The pipeline already upscales with Real-ESRGAN
    before the final cut, so this costs nothing it was not already paying.

  * duration. H3 emits 24fps and only accepts frame counts of 5 + 17n, so
    "10 seconds" becomes 243 frames (10.125s).

Endpoints
    GET  /health                          -> {"status":"ok","model":...,"running":...}
    POST /v1/video/generations            -> {"task_id":...,"base_resp":{...}}
    GET  /v1/query/video_generation       -> status / video_url / file_id
    GET  /v1/files/{file_id}              -> mp4 bytes
    GET  /v1/files/retrieve?file_id=...   -> {"file":{"download_url":...}}
    POST /v1/video/cancel                 -> {"task_id":...,"status":"Fail"}
"""

import argparse
import base64
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Bookreel H3 video", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# h3.c mechanical limits, from its README: canvas sides are multiples of 32 and
# the budget is the PRODUCT 768*1344 -- not width 768 by height 1344, which is
# why landscape can go wider than 768. Frames align upward to 5 + 17n at 24fps.
# Fixed, so the console can open one ComfyUI websocket under the same id and
# see per-step progress; a per-task id gave the page nothing to subscribe to.
H3_CLIENT = "bookreel-h3"

MAX_SIDE = 1344
PIXEL_BUDGET = 768 * 1344
ASPECT_TOLERANCE = 0.01
FPS = 24

CFG = {
    "backend": "h3c",
    # The ceiling on the canvas. h3.c's own limit is 768*1344; rendering time
    # scales with it hard (attention is over frames x pixels), so a slow GPU
    # can trade resolution for wall clock. Everything is upscaled before the
    # final cut anyway.
    "pixel_budget": PIXEL_BUDGET,
    "bin": "",
    "model": "",
    "out_dir": "",
    "steps": 20,
    "layers": 45,
    "reuse": 2,
    "extra": [],
    "keep": 20,
    # --- comfy backend ---
    "comfy": "http://127.0.0.1:7860",
    "c_unet": "minimax_h3_fl2va_pruned_fp8_scaled.safetensors",
    "c_clip": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    "c_vae": "minimax_h3_video_vae_fp16.safetensors",
    # MiniMax-H3 is an audio+video model: the sampler's latent carries a
    # soundtrack, and it is silently thrown away unless a second VAE decodes
    # it and CreateVideo is given the result. Empty = silent clips.
    "c_audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "c_lora": "minimax_h3_fl2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors",
    "c_lora_strength": 1.0,
    "c_dtype": "default",
    "c_cfg": 1.0,
    "c_sampler": "euler",
    "c_scheduler": "simple",
    "c_shift_v": 12.0,
    "c_shift_a": 3.0,
}

_TASKS = {}                 # task_id -> dict
_LOCK = threading.Lock()
_QUEUE = queue.Queue()
_RUNNING = {"task_id": None, "proc": None, "prompt_id": None}

OK = {"status_code": 0, "status_msg": "success"}


# ----------------------------------------------------------------------
# shaping the request into something h3.c will accept
# ----------------------------------------------------------------------
def _legal_canvas(width, height):
    """Biggest legal h3.c canvas that keeps the requested aspect ratio.

    Two things matter and they pull against each other: keeping the aspect
    ratio (the wrong one means distorted faces or a crop somebody has to fix in
    the edit) and keeping the pixels (everything gets upscaled later, but you
    cannot upscale detail that was never rendered). Ranking by ratio alone
    picks absurdly small exact matches - 1920x1080 would land on 512x288 - so
    take every candidate within 1% of the requested ratio and pick the largest
    of those. Only if nothing is that close do we fall back to closest ratio.
    """
    if int(width) <= 0 or int(height) <= 0:
        return 704, 1280
    want = float(width) / float(height)
    sides = range(32, MAX_SIDE + 32, 32)
    close, all_cands = [], []
    for w in sides:
        for h in sides:
            if w * h > (CFG.get("pixel_budget") or PIXEL_BUDGET):
                continue
            err = abs(w / float(h) - want) / want
            all_cands.append((err, -(w * h), w, h))
            if err <= ASPECT_TOLERANCE:
                close.append((-(w * h), err, w, h))
    if close:
        close.sort()
        return close[0][2], close[0][3]
    all_cands.sort()
    return all_cands[0][2], all_cands[0][3]


def _parse_resolution(text, fallback):
    if not text:
        return fallback
    m = re.match(r"^\s*(\d+)\s*[xX*]\s*(\d+)\s*$", str(text))
    if not m:
        return fallback
    return _legal_canvas(int(m.group(1)), int(m.group(2)))


def _align_frames(seconds):
    """H3 only accepts 5 + 17n frames. Round up, never down: a clip that comes
    back short cannot be stretched in the edit, a long one just gets trimmed."""
    want = max(1, int(round(float(seconds) * FPS)))
    n = 0
    while 5 + 17 * n < want:
        n += 1
    return 5 + 17 * n


def _materialize_image(ref, dest):
    """The console hands us whatever ComfyUI gave it: an http URL, a data: URL
    or a local path. h3.c wants a file on disk."""
    if not ref:
        return None
    ref = str(ref)
    if ref.startswith("data:"):
        head, _, payload = ref.partition(",")
        raw = base64.b64decode(payload) if "base64" in head else payload.encode("utf-8")
        with open(dest, "wb") as f:
            f.write(raw)
        return dest
    if ref.startswith("http://") or ref.startswith("https://"):
        import urllib.request
        # Reference plates are named by time of day - loca_lo01_夜_int_front.png -
        # and the console passes the URL with the characters raw. urlopen only
        # speaks ASCII; percent-encode the path and query, leave the host alone.
        parts = urllib.parse.urlsplit(ref)
        ref = urllib.parse.urlunsplit((parts.scheme, parts.netloc,
                                       urllib.parse.quote(parts.path, safe="/%"),
                                       urllib.parse.quote(parts.query, safe="=&%"), ""))
        with urllib.request.urlopen(ref, timeout=30) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return dest
    if os.path.isfile(ref):
        return os.path.abspath(ref)
    raise ValueError("cannot read first_frame_image: %s" % ref[:120])


# ----------------------------------------------------------------------
# the ComfyUI backend
# ----------------------------------------------------------------------
def _comfy_get(path, timeout=30):
    with urllib.request.urlopen(CFG["comfy"] + path, timeout=timeout) as r:
        return json.load(r)


def _comfy_post(path, payload, timeout=60):
    req = urllib.request.Request(CFG["comfy"] + path,
                                 json.dumps(payload).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _comfy_alive():
    try:
        _comfy_get("/system_stats", timeout=5)
        return True
    except Exception:                                  # noqa: BLE001
        return False


def _comfy_upload_image(path):
    """Put the first frame in ComfyUI's input dir so LoadImage can read it.

    Hand-rolled multipart: this module deliberately has no requests dependency
    (it ships in the tools venv, which stays small), and the body is two fields.
    Returns the name ComfyUI actually stored it under, which may be suffixed if
    it collided with an existing file.
    """
    name = os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()
    boundary = "----bookreel%s" % uuid.uuid4().hex
    pre = ("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
           "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode("utf-8")
    mid = ("\r\n--%s\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n"
           "--%s--\r\n" % (boundary, boundary)).encode("utf-8")
    req = urllib.request.Request(
        CFG["comfy"] + "/upload/image", pre + blob + mid,
        {"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    with urllib.request.urlopen(req, timeout=120) as r:
        info = json.load(r)
    stored = info.get("name") or name
    if info.get("subfolder"):
        stored = "%s/%s" % (info["subfolder"], stored)
    return stored


def _comfy_workflow(task, image_name):
    """The graph validated by hand against ComfyUI's own H3 nodes.

    Node 6 (MiniMaxH3ImageToVideo) emits positive conditioning AND the joint
    video+audio latent, so the latent must come from it rather than an Empty
    node -- it carries the keyframe conditioning with it. There is no negative
    output, hence ConditioningZeroOut: with the turbo LoRA cfg is 1.0 and the
    negative branch is unused anyway, but KSampler still requires the input.
    """
    g = {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": CFG["c_unet"], "weight_dtype": CFG["c_dtype"]}},
        "4": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": CFG["c_clip"], "type": "minimax"}},
        "5": {"class_type": "VAELoader", "inputs": {"vae_name": CFG["c_vae"]}},
        "6": {"class_type": "MiniMaxH3ImageToVideo",
              "inputs": {"clip": ["4", 0], "vae": ["5", 0], "prompt": task["prompt"],
                         "width": task["width"], "height": task["height"],
                         "length": task["frames"]}},
        "7": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["6", 0]}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["5", 0]}},
        "10": {"class_type": "CreateVideo", "inputs": {"images": ["9", 0], "fps": float(FPS)}},
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0], "filename_prefix": "bookreel/" + task["id"],
                          "format": "auto", "codec": "auto"}},
    }
    model_ref = ["1", 0]
    if CFG["c_lora"]:
        g["2"] = {"class_type": "LoraLoaderModelOnly",
                  "inputs": {"model": model_ref, "lora_name": CFG["c_lora"],
                             "strength_model": CFG["c_lora_strength"]}}
        model_ref = ["2", 0]
    g["3"] = {"class_type": "MiniMaxH3SigmaShift",
              "inputs": {"model": model_ref, "shift_video": CFG["c_shift_v"],
                         "shift_audio": CFG["c_shift_a"]}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["3", 0], "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["6", 1],
                         "seed": task["seed"] if task["seed"] is not None else 0,
                         "steps": CFG["steps"], "cfg": CFG["c_cfg"],
                         "sampler_name": CFG["c_sampler"],
                         "scheduler": CFG["c_scheduler"], "denoise": 1.0}}
    if CFG.get("c_audio_vae"):
        g["13"] = {"class_type": "VAELoader", "inputs": {"vae_name": CFG["c_audio_vae"]}}
        g["14"] = {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["13", 0]}}
        g["10"]["inputs"]["audio"] = ["14", 0]
    if image_name:
        g["12"] = {"class_type": "LoadImage", "inputs": {"image": image_name}}
        g["6"]["inputs"]["first_frame"] = ["12", 0]
    return g


def _comfy_fetch_output(entry, dest):
    """Find the SaveVideo result in a /history entry and write it to dest."""
    for _node, out in (entry.get("outputs") or {}).items():
        for key in ("images", "video", "videos", "gifs"):
            for item in (out.get(key) or []):
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                q = urllib.parse.urlencode({"filename": item["filename"],
                                            "subfolder": item.get("subfolder", ""),
                                            "type": item.get("type", "output")})
                with urllib.request.urlopen(CFG["comfy"] + "/view?" + q, timeout=300) as r, \
                        open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return True
    return False


def _run_comfy(task):
    """Submit one job and block until ComfyUI finishes it.

    Blocking is intentional and matches the h3.c worker: a single render
    saturates the GPU, so the queue here stays one-at-a-time regardless of what
    ComfyUI would accept.
    """
    task_id = task["id"]
    image_name = None
    if task.get("first_frame"):
        image_name = _comfy_upload_image(task["first_frame"])

    graph = _comfy_workflow(task, image_name)
    res = _comfy_post("/prompt", {"prompt": graph, "client_id": H3_CLIENT})
    prompt_id = res.get("prompt_id")
    if not prompt_id:
        return False, "ComfyUI refused the graph: %s" % json.dumps(res)[:300]
    _RUNNING["prompt_id"] = prompt_id
    print("[h3] %s -> comfy prompt %s" % (task_id, prompt_id), flush=True)

    unreachable_since = None
    while True:
        with _LOCK:
            if _TASKS.get(task_id, {}).get("cancelled"):
                return False, "cancelled"
        time.sleep(2)
        try:
            hist = _comfy_get("/history/%s" % prompt_id, timeout=20)
            unreachable_since = None
        except Exception as exc:                       # noqa: BLE001
            # ComfyUI's HTTP thread goes quiet while the VAE decodes 73 frames;
            # one timed-out poll is not a dead server. Only give up after it has
            # been unreachable for a while - and the render finished anyway once,
            # unseen, because a single timeout was treated as fatal.
            if unreachable_since is None:
                unreachable_since = time.time()
            if time.time() - unreachable_since > 600:
                return False, "ComfyUI unreachable mid-render: %s" % exc
            time.sleep(5)
            continue
        entry = hist.get(prompt_id)
        if not entry:
            continue
        status = (entry.get("status") or {})
        if status.get("status_str") == "error" or (
                status.get("completed") is False and status.get("status_str") != "success"):
            msgs = status.get("messages") or []
            detail = next((json.dumps(m[1])[:400] for m in msgs
                           if m and m[0] == "execution_error"), json.dumps(status)[:300])
            return False, "ComfyUI execution error: %s" % detail
        if not _comfy_fetch_output(entry, task["path"]):
            return False, "ComfyUI finished but produced no video output"
        return True, None


# ----------------------------------------------------------------------
# the worker
# ----------------------------------------------------------------------
def _set(task_id, **kw):
    with _LOCK:
        t = _TASKS.get(task_id)
        if t:
            t.update(kw)


def _build_argv(task):
    argv = [CFG["bin"], "-d", CFG["model"], "-p", task["prompt"],
            "-o", task["path"],
            "--width", str(task["width"]), "--height", str(task["height"]),
            "--frames", str(task["frames"]),
            "--steps", str(CFG["steps"]), "--layers", str(CFG["layers"]),
            "--reuse", str(CFG["reuse"])]
    if task.get("first_frame"):
        argv += ["--first-frame", task["first_frame"]]
    if task.get("seed") is not None:
        argv += ["--seed", str(task["seed"])]
    argv += CFG["extra"]
    return argv


def _worker():
    while True:
        task_id = _QUEUE.get()
        with _LOCK:
            task = _TASKS.get(task_id)
        if not task or task["status"] == "Fail":
            _QUEUE.task_done()
            continue

        _set(task_id, status="Processing", started=time.time())
        _RUNNING.update(task_id=task_id, proc=None, prompt_id=None)
        try:
            if CFG["backend"] == "comfy":
                ok, err = _run_comfy(task)
            else:
                ok, err = _run_h3c(task)
        except Exception as exc:                       # noqa: BLE001
            ok, err = False, "%s backend raised: %s" % (CFG["backend"], exc)
        _RUNNING.update(task_id=None, proc=None, prompt_id=None)

        with _LOCK:
            cancelled = _TASKS.get(task_id, {}).get("cancelled")
        took = int(time.time() - task["started"])
        if cancelled:
            _set(task_id, status="Fail", error="cancelled", took=took)
            print("[h3] %s cancelled after %ds" % (task_id, took), flush=True)
        elif ok and os.path.isfile(task["path"]):
            _set(task_id, status="Success", took=took)
            print("[h3] %s done in %ds -> %s" % (task_id, took, task["path"]), flush=True)
        else:
            _set(task_id, status="Fail", took=took, error=err or "no video produced")
            print("[h3] %s failed: %s" % (task_id, err), flush=True)
        _prune()
        _QUEUE.task_done()


def _run_h3c(task):
    """Shell out to the h3 binary. Returns (ok, error)."""
    task_id = task["id"]
    argv = _build_argv(task)
    print("[h3] %s start: %s" % (task_id, " ".join(argv[:9])), flush=True)
    log_path = os.path.join(CFG["out_dir"], task_id + ".log")
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
            _RUNNING["proc"] = proc
            rc = proc.wait()
    except Exception as exc:                           # noqa: BLE001
        return False, "could not launch h3: %s" % exc
    if rc == 0:
        return True, None
    return False, "h3 exited %s -- see %s" % (rc, log_path)


def _prune():
    """Keep the last N finished renders on disk; a chapter is 40+ clips and
    each one is a real mp4."""
    with _LOCK:
        done = [(t["created"], t["id"]) for t in _TASKS.values()
                if t["status"] in ("Success", "Fail")]
    done.sort()
    for _, tid in done[:-CFG["keep"]]:
        with _LOCK:
            t = _TASKS.pop(tid, None)
        if not t:
            continue
        for p in (t.get("path"), os.path.join(CFG["out_dir"], tid + ".log")):
            try:
                if p and os.path.isfile(p):
                    os.remove(p)
            except OSError:
                pass


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
class GenReq(BaseModel):
    model: Optional[str] = None
    prompt: str = ""
    first_frame_image: Optional[str] = None
    last_frame_image: Optional[str] = None
    duration: Optional[float] = 10
    resolution: Optional[str] = None
    seed: Optional[int] = None
    prompt_optimizer: Optional[bool] = False


@app.get("/health")
def health():
    with _LOCK:
        queued = sum(1 for t in _TASKS.values() if t["status"] == "Queued")
    common = {
        # CFG's budget, not the module ceiling: --max-pixels lowers the canvas
        # to buy frames, and reporting the constant made /health claim 1032192
        # while the renderer was actually drawing 1088x608.
        "max_pixels": CFG.get("pixel_budget") or PIXEL_BUDGET,
        "max_side": MAX_SIDE,
        "fps": FPS,
        "steps": CFG["steps"],
        "running": _RUNNING["task_id"],
        "queued": queued,
    }
    if CFG["backend"] == "comfy":
        alive = _comfy_alive()
        common.update(status="ok" if alive else "no-comfy", engine="comfyui",
                      comfy=CFG["comfy"], comfy_reachable=alive,
                      unet=CFG["c_unet"], clip=CFG["c_clip"], vae=CFG["c_vae"],
                      lora=CFG["c_lora"] or None, cfg=CFG["c_cfg"],
                      sampler=CFG["c_sampler"])
        return common
    common.update(status="ok" if os.path.isfile(CFG["bin"]) else "no-binary",
                  engine="h3.c", model=CFG["model"], bin=CFG["bin"],
                  layers=CFG["layers"], reuse=CFG["reuse"])
    return common


@app.post("/v1/video/generations")
def generate(req: GenReq, request: Request):
    if CFG["backend"] == "comfy":
        if not _comfy_alive():
            raise HTTPException(status_code=503,
                                detail="ComfyUI not reachable at %s" % CFG["comfy"])
    else:
        if not os.path.isfile(CFG["bin"]):
            raise HTTPException(status_code=503, detail="h3 binary not found at %s" % CFG["bin"])
        if not os.path.isdir(CFG["model"]):
            raise HTTPException(status_code=503, detail="H3 weights not found at %s" % CFG["model"])
    if not (req.prompt or "").strip():
        raise HTTPException(status_code=400, detail="prompt is empty")

    task_id = uuid.uuid4().hex[:16]
    width, height = _parse_resolution(req.resolution, (704, 1280))
    frames = _align_frames(req.duration or 10)

    first = None
    if req.first_frame_image:
        try:
            first = _materialize_image(req.first_frame_image,
                                       os.path.join(CFG["out_dir"], task_id + "-first.png"))
        except Exception as exc:                       # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(exc))
    if req.last_frame_image:
        print("[h3] note: last_frame_image ignored -- h3.c cannot mix it with"
              " first-frame conditioning on this path", flush=True)

    task = {
        "id": task_id,
        "status": "Queued",
        "created": time.time(),
        "started": None,
        "prompt": req.prompt.strip(),
        "width": width, "height": height, "frames": frames,
        "seed": req.seed,
        "first_frame": first,
        "path": os.path.join(CFG["out_dir"], task_id + ".mp4"),
        "error": None,
        "cancelled": False,
        "took": None,
    }
    with _LOCK:
        _TASKS[task_id] = task
    _QUEUE.put(task_id)

    print("[h3] queued %s  %dx%d  %d frames (%.2fs)  \"%s\"" %
          (task_id, width, height, frames, frames / float(FPS), task["prompt"][:60]),
          flush=True)
    return {"task_id": task_id, "base_resp": OK}


@app.get("/v1/query/video_generation")
def query(task_id: str, request: Request):
    with _LOCK:
        task = _TASKS.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="unknown task_id")

    out = {
        "task_id": task_id,
        "status": task["status"],
        "base_resp": OK,
        "canvas": "%dx%d" % (task["width"], task["height"]),
        "frames": task["frames"],
    }
    if task["status"] == "Processing" and task["started"]:
        out["elapsed"] = int(time.time() - task["started"])
    if task["status"] == "Success":
        # Absolute URL so the console can drop it straight into a <video src>.
        base = str(request.base_url).rstrip("/")
        out["file_id"] = task_id
        out["video_url"] = "%s/v1/files/%s" % (base, task_id)
        out["took"] = task["took"]
    if task["status"] == "Fail":
        out["error"] = task["error"]
    return out


@app.get("/v1/files/{file_id}")
def file_bytes(file_id: str):
    with _LOCK:
        task = _TASKS.get(file_id)
    path = task["path"] if task else os.path.join(CFG["out_dir"], file_id + ".mp4")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="no video for %s" % file_id)
    return FileResponse(path, media_type="video/mp4", filename=os.path.basename(path))


@app.get("/v1/files/retrieve")
def file_retrieve(file_id: str, request: Request):
    base = str(request.base_url).rstrip("/")
    return {"file": {"file_id": file_id,
                     "download_url": "%s/v1/files/%s" % (base, file_id)},
            "base_resp": OK}


class CancelReq(BaseModel):
    task_id: str


@app.post("/v1/video/cancel")
def cancel(req: CancelReq):
    with _LOCK:
        task = _TASKS.get(req.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="unknown task_id")
        task["cancelled"] = True
        if task["status"] == "Queued":
            task["status"] = "Fail"
            task["error"] = "cancelled before it started"
    if _RUNNING["task_id"] == req.task_id:
        if _RUNNING.get("proc"):
            try:
                _RUNNING["proc"].send_signal(signal.SIGTERM)
            except Exception:                          # noqa: BLE001
                pass
        elif CFG["backend"] == "comfy":
            # /interrupt stops whatever is mid-execution; the worker sees the
            # cancelled flag on its next poll and marks the task failed.
            try:
                _comfy_post("/interrupt", {}, timeout=10)
            except Exception:                          # noqa: BLE001
                pass
    return {"task_id": req.task_id, "status": "Fail", "base_resp": OK}


def main():
    ap = argparse.ArgumentParser(description="Bookreel local video service (h3.c / MiniMax-H3)")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--bin", default=os.path.normpath(os.path.join(here, "..", "third_party", "h3.c", "h3")),
                    help="the h3 binary built by `make` in the h3.c checkout")
    ap.add_argument("--model", default=os.path.normpath(os.path.join(here, "..", "models", "minimax-h3")),
                    help="MiniMax-H3 Hugging Face snapshot directory")
    ap.add_argument("--out", default=os.path.normpath(os.path.join(here, "..", "output", "video")))
    ap.add_argument("--backend", choices=("h3c", "comfy"), default="h3c",
                    help="h3c = antirez's Metal binary (Apple Silicon only); "
                         "comfy = ComfyUI's built-in MiniMax-H3 nodes (any GPU ComfyUI supports)")
    ap.add_argument("--steps", type=int, default=None,
                    help="denoise passes. Default 20 for h3c, or 4 for comfy when the "
                         "turbo LoRA is in use (that is what the LoRA is trained for)")
    ap.add_argument("--layers", type=int, default=45, help="active DiT blocks, 40-50")
    ap.add_argument("--reuse", type=int, default=2, help="velocity reuse; 1 is best quality")
    ap.add_argument("--token-reduction", action="store_true",
                    help="pair horizontal tokens in the middle blocks - faster, can shift composition")
    ap.add_argument("--keep", type=int, default=20, help="finished renders to keep on disk")
    ap.add_argument("--max-pixels", type=int, default=PIXEL_BUDGET,
                    help="canvas ceiling in pixels (default %d = 768*1344); lower it to trade "
                         "resolution for speed" % PIXEL_BUDGET)

    c = ap.add_argument_group("comfy backend")
    c.add_argument("--comfy", default="http://127.0.0.1:7860", help="ComfyUI base URL")
    c.add_argument("--comfy-unet", default=CFG["c_unet"], help="file in ComfyUI/models/diffusion_models")
    c.add_argument("--comfy-clip", default=CFG["c_clip"], help="file in ComfyUI/models/text_encoders")
    c.add_argument("--comfy-vae", default=CFG["c_vae"], help="video VAE in ComfyUI/models/vae")
    c.add_argument("--comfy-audio-vae", default=CFG["c_audio_vae"],
                   help="audio VAE in ComfyUI/models/vae; empty string makes silent clips")
    c.add_argument("--comfy-lora", default=CFG["c_lora"],
                   help="turbo LoRA in ComfyUI/models/loras; empty string disables it")
    c.add_argument("--lora-strength", type=float, default=1.0)
    c.add_argument("--weight-dtype", default="default",
                   choices=("default", "fp8_e4m3fn", "fp8_e4m3fn_fast", "fp8_e5m2"))
    c.add_argument("--cfg", type=float, default=1.0, help="1.0 is right with a turbo LoRA")
    c.add_argument("--sampler", default="euler")
    c.add_argument("--scheduler", default="simple")
    c.add_argument("--shift-video", type=float, default=12.0)
    c.add_argument("--shift-audio", type=float, default=3.0)
    args = ap.parse_args()

    lora = (args.comfy_lora or "").strip()
    if args.steps is not None:
        steps = args.steps
    elif args.backend == "comfy":
        steps = 4 if lora else 20
    else:
        steps = 20

    CFG.update(
        backend=args.backend,
        bin=os.path.abspath(args.bin),
        model=os.path.abspath(args.model),
        out_dir=os.path.abspath(args.out),
        steps=steps, layers=args.layers, reuse=args.reuse,
        extra=(["--token-reduction"] if args.token_reduction else []),
        keep=max(1, args.keep), pixel_budget=max(32 * 32, min(int(args.max_pixels), PIXEL_BUDGET)),
        comfy=args.comfy.rstrip("/"),
        c_unet=args.comfy_unet, c_clip=args.comfy_clip, c_vae=args.comfy_vae,
        c_audio_vae=(args.comfy_audio_vae or "").strip(),
        c_lora=lora, c_lora_strength=args.lora_strength,
        c_dtype=args.weight_dtype, c_cfg=args.cfg,
        c_sampler=args.sampler, c_scheduler=args.scheduler,
        c_shift_v=args.shift_video, c_shift_a=args.shift_audio,
    )
    os.makedirs(CFG["out_dir"], exist_ok=True)

    print("[h3] backend   : %s" % CFG["backend"], flush=True)
    print("[h3] output    : %s" % CFG["out_dir"], flush=True)
    if CFG["pixel_budget"] != PIXEL_BUDGET:
        print("[h3] canvas    : capped at %d pixels (h3 limit %d)" % (CFG["pixel_budget"], PIXEL_BUDGET), flush=True)
    if CFG["backend"] == "comfy":
        print("[h3] comfy     : %s" % CFG["comfy"], flush=True)
        print("[h3] unet      : %s" % CFG["c_unet"], flush=True)
        print("[h3] clip      : %s" % CFG["c_clip"], flush=True)
        print("[h3] vae       : %s" % CFG["c_vae"], flush=True)
        print("[h3] audio vae : %s" % (CFG["c_audio_vae"] or "(none - silent clips)"), flush=True)
        print("[h3] lora      : %s" % (CFG["c_lora"] or "(none)"), flush=True)
        print("[h3] preset    : steps %d, cfg %.1f, %s/%s"
              % (CFG["steps"], CFG["c_cfg"], CFG["c_sampler"], CFG["c_scheduler"]), flush=True)
        if not _comfy_alive():
            print("[h3] ! ComfyUI not answering at %s -- start slot B first"
                  % CFG["comfy"], flush=True)
    else:
        print("[h3] binary    : %s" % CFG["bin"], flush=True)
        print("[h3] weights   : %s" % CFG["model"], flush=True)
        print("[h3] preset    : steps %d, layers %d, reuse %d%s"
              % (CFG["steps"], CFG["layers"], CFG["reuse"],
                 ", token-reduction" if CFG["extra"] else ""), flush=True)
        if not os.path.isfile(CFG["bin"]):
            print("[h3] ! binary missing -- build it: cd third_party/h3.c && make -j8", flush=True)
        if not os.path.isdir(CFG["model"]):
            print("[h3] ! weights missing -- menu V downloads them (~144GB)", flush=True)
        if not shutil.which("ffmpeg"):
            print("[h3] ! ffmpeg not on PATH -- h3.c needs it to encode the mp4", flush=True)
    print("[h3] listening : http://%s:%d" % (args.host, args.port), flush=True)
    print("[h3] one render at a time on purpose; extra jobs queue.", flush=True)

    threading.Thread(target=_worker, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
