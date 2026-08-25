# -*- coding: utf-8 -*-
"""Bookreel . local video service (slot C on Apple Silicon).

Wraps antirez's h3.c -- a native MiniMax-H3 engine for Metal -- in the same
HTTP shape the web console already speaks to MiniMax's hosted API, so slot C
works on a Mac with no console change:

    POST /v1/video/generations           -> {"task_id": ...}
    GET  /v1/query/video_generation      -> {"status": "Success", "video_url": ...}
    GET  /v1/files/{file_id}             -> the mp4 itself

    python h3_server.py --bin ../third_party/h3.c/h3 --model ../models/minimax-h3

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
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
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
MAX_SIDE = 1344
PIXEL_BUDGET = 768 * 1344
ASPECT_TOLERANCE = 0.01
FPS = 24

CFG = {
    "bin": "",
    "model": "",
    "out_dir": "",
    "steps": 20,
    "layers": 45,
    "reuse": 2,
    "extra": [],
    "keep": 20,
}

_TASKS = {}                 # task_id -> dict
_LOCK = threading.Lock()
_QUEUE = queue.Queue()
_RUNNING = {"task_id": None, "proc": None}

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
            if w * h > PIXEL_BUDGET:
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
        with urllib.request.urlopen(ref, timeout=30) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return dest
    if os.path.isfile(ref):
        return os.path.abspath(ref)
    raise ValueError("cannot read first_frame_image: %s" % ref[:120])


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
        argv = _build_argv(task)
        print("[h3] %s start: %s" % (task_id, " ".join(argv[:9])), flush=True)
        log_path = os.path.join(CFG["out_dir"], task_id + ".log")
        try:
            with open(log_path, "wb") as log:
                proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT)
                _RUNNING.update(task_id=task_id, proc=proc)
                rc = proc.wait()
        except Exception as exc:                       # noqa: BLE001
            _set(task_id, status="Fail", error="could not launch h3: %s" % exc)
            print("[h3] %s launch failed: %s" % (task_id, exc), flush=True)
            _RUNNING.update(task_id=None, proc=None)
            _QUEUE.task_done()
            continue
        _RUNNING.update(task_id=None, proc=None)

        with _LOCK:
            cancelled = _TASKS.get(task_id, {}).get("cancelled")
        took = int(time.time() - task["started"])
        if cancelled:
            _set(task_id, status="Fail", error="cancelled", took=took)
            print("[h3] %s cancelled after %ds" % (task_id, took), flush=True)
        elif rc == 0 and os.path.isfile(task["path"]):
            _set(task_id, status="Success", took=took)
            print("[h3] %s done in %ds -> %s" % (task_id, took, task["path"]), flush=True)
        else:
            _set(task_id, status="Fail", took=took,
                 error="h3 exited %s -- see %s" % (rc, log_path))
            print("[h3] %s failed (rc=%s), see %s" % (task_id, rc, log_path), flush=True)
        _prune()
        _QUEUE.task_done()


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
    return {
        "status": "ok" if os.path.isfile(CFG["bin"]) else "no-binary",
        "engine": "h3.c",
        "model": CFG["model"],
        "bin": CFG["bin"],
        "max_pixels": PIXEL_BUDGET,
        "max_side": MAX_SIDE,
        "steps": CFG["steps"], "layers": CFG["layers"], "reuse": CFG["reuse"],
        "running": _RUNNING["task_id"],
        "queued": queued,
    }


@app.post("/v1/video/generations")
def generate(req: GenReq, request: Request):
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
    if _RUNNING["task_id"] == req.task_id and _RUNNING["proc"]:
        try:
            _RUNNING["proc"].send_signal(signal.SIGTERM)
        except Exception:                              # noqa: BLE001
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
    ap.add_argument("--steps", type=int, default=20, help="denoise passes; 4 is the fast preview, 50 the reference")
    ap.add_argument("--layers", type=int, default=45, help="active DiT blocks, 40-50")
    ap.add_argument("--reuse", type=int, default=2, help="velocity reuse; 1 is best quality")
    ap.add_argument("--token-reduction", action="store_true",
                    help="pair horizontal tokens in the middle blocks - faster, can shift composition")
    ap.add_argument("--keep", type=int, default=20, help="finished renders to keep on disk")
    args = ap.parse_args()

    CFG.update(
        bin=os.path.abspath(args.bin),
        model=os.path.abspath(args.model),
        out_dir=os.path.abspath(args.out),
        steps=args.steps, layers=args.layers, reuse=args.reuse,
        extra=(["--token-reduction"] if args.token_reduction else []),
        keep=max(1, args.keep),
    )
    os.makedirs(CFG["out_dir"], exist_ok=True)

    print("[h3] binary    : %s" % CFG["bin"], flush=True)
    print("[h3] weights   : %s" % CFG["model"], flush=True)
    print("[h3] output    : %s" % CFG["out_dir"], flush=True)
    print("[h3] preset    : steps %d, layers %d, reuse %d%s"
          % (CFG["steps"], CFG["layers"], CFG["reuse"],
             ", token-reduction" if CFG["extra"] else ""), flush=True)
    if not os.path.isfile(CFG["bin"]):
        print("[h3] ! binary missing -- build it: cd third_party/h3.c && make -j8", flush=True)
    if not os.path.isdir(CFG["model"]):
        print("[h3] ! weights missing -- menu V downloads them (~40GB)", flush=True)
    if not shutil.which("ffmpeg"):
        print("[h3] ! ffmpeg not on PATH -- h3.c needs it to encode the mp4", flush=True)
    print("[h3] listening : http://%s:%d" % (args.host, args.port), flush=True)
    print("[h3] one render at a time on purpose; extra jobs queue.", flush=True)

    threading.Thread(target=_worker, daemon=True).start()

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
