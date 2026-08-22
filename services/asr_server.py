# -*- coding: utf-8 -*-
"""Bookreel · FunASR forced-alignment service (slot: 对齐).

Takes narration audio plus the text it was read from, and returns the
timestamp of every sentence -- which is what turns a wav into an SRT you can
burn onto the finished cut.

    python asr_server.py --port 9101 --device cpu

Endpoints
    GET  /health   -> readiness + which FunASR model is in use
    POST /align    -> multipart: audio=<file>, text=<the narration text>
                      returns {"sentences":[{"index","text","start","end"}],
                               "srt": "...", "duration": float}
    POST /srt      -> same input, returns the SRT as a downloadable file

FunASR pulls its weights from ModelScope on first call, so the first request
is slow and needs a network connection. Everything after that is local.
"""

import argparse
import os
import re
import sys
import tempfile
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from typing import List

# FastAPI needs python-multipart to accept file uploads. Without it, merely
# declaring the /align route raises at import and the whole process dies with a
# traceback. Detect it up front so the server still starts and /health can say
# what is wrong -- much easier to diagnose than a stack trace at boot.
try:
    import multipart  # noqa: F401
    _MULTIPART = True
except ImportError:
    try:
        import python_multipart  # noqa: F401
        _MULTIPART = True
    except ImportError:
        _MULTIPART = False

_NO_MULTIPART_MSG = (
    "python-multipart is not installed in venv-audio, so file upload routes "
    "are disabled. Fix it from the Bookreel menu: R (Repair deps). Or by hand: "
    "venvs\\audio\\Scripts\\activate.bat && pip install python-multipart"
)

app = FastAPI(title="Bookreel Forced Alignment (FunASR)", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CFG = {"device": "cpu", "model": "fa-zh", "cache": ""}
_STATE = {"model": None, "error": None}
_LOCK = threading.Lock()
_RUN_LOCK = threading.Lock()

# Split on CJK and latin sentence enders, keeping the punctuation attached.
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_PUNCT = re.compile(r"[\s，。！？、；：“”‘’（）【】《》…—,.!?;:\"'()\[\]<>-]")


def _load():
    if _STATE["model"] is not None or _STATE["error"] is not None:
        return
    with _LOCK:
        if _STATE["model"] is not None or _STATE["error"] is not None:
            return
        try:
            from funasr import AutoModel
        except ImportError as exc:
            _STATE["error"] = (
                "funasr is not installed in this venv. Run install.bat step [2/4] "
                "(venv-audio). Detail: %s" % exc
            )
            return
        t0 = time.time()
        print("[asr] loading FunASR %r on %s (first run downloads weights) ..."
              % (CFG["model"], CFG["device"]), flush=True)
        try:
            kwargs = {"model": CFG["model"], "device": CFG["device"], "disable_update": True}
            if CFG["cache"]:
                kwargs["cache_dir"] = CFG["cache"]
            _STATE["model"] = AutoModel(**kwargs)
        except Exception as exc:  # noqa: BLE001
            _STATE["error"] = (
                "failed to load FunASR %r: %s -- check the network, ModelScope "
                "must be reachable on first run" % (CFG["model"], exc)
            )
            print("[asr] load failed: %s" % exc, flush=True)
            return
        print("[asr] ready in %.1fs" % (time.time() - t0), flush=True)


def _split_sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p and p.strip()]
    return parts or ([text.strip()] if text and text.strip() else [])


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def _build_srt(sentences) -> str:
    lines = []
    for i, sent in enumerate(sentences, 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (_fmt_ts(sent["start"]), _fmt_ts(sent["end"])))
        lines.append(sent["text"])
        lines.append("")
    return "\n".join(lines)


def _align(audio_path: str, text: str):
    """Run FunASR forced alignment and fold char timestamps into sentences."""
    _load()
    if _STATE["error"]:
        raise HTTPException(status_code=503, detail=_STATE["error"])

    sentences = _split_sentences(text)
    if not sentences:
        raise HTTPException(status_code=400, detail="text is empty")

    with _RUN_LOCK:
        try:
            res = _STATE["model"].generate(input=(audio_path, text), data_type=("sound", "text"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="alignment failed: %s" % exc)

    if not res:
        raise HTTPException(status_code=500, detail="aligner returned nothing")

    # fa-zh returns per-character spans in milliseconds: [[start, end], ...]
    stamps = res[0].get("timestamp") or []
    if not stamps:
        raise HTTPException(
            status_code=500,
            detail="aligner returned no timestamps -- check that the audio and text match",
        )

    # Character stream that the aligner actually saw (punctuation stripped).
    flat = _PUNCT.sub("", "".join(sentences))
    if len(stamps) < len(flat):
        print("[asr] warn: %d stamps for %d chars, tail will be approximated"
              % (len(stamps), len(flat)), flush=True)

    out = []
    cursor = 0
    for i, sent in enumerate(sentences):
        n = len(_PUNCT.sub("", sent))
        if n == 0:
            continue
        lo = min(cursor, len(stamps) - 1)
        hi = min(cursor + n - 1, len(stamps) - 1)
        out.append({
            "index": i + 1,
            "text": sent,
            "start": round(float(stamps[lo][0]) / 1000.0, 3),
            "end": round(float(stamps[hi][1]) / 1000.0, 3),
            "chars": n,
        })
        cursor += n

    duration = round(float(stamps[-1][1]) / 1000.0, 3) if stamps else 0.0
    return out, duration


async def _save_upload(audio: UploadFile) -> str:
    suffix = os.path.splitext(audio.filename or "")[1] or ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="bookreel-align-")
    os.close(fd)
    data = await audio.read()
    if not data:
        os.unlink(path)
        raise HTTPException(status_code=400, detail="audio file is empty")
    with open(path, "wb") as fh:
        fh.write(data)
    return path


@app.get("/health")
def health():
    return {
        "status": "ok",
        "engine": "funasr",
        "model": CFG["model"],
        "device": CFG["device"],
        "loaded": _STATE["model"] is not None,
        "upload_routes": _MULTIPART,
        "error": _STATE["error"] or (None if _MULTIPART else _NO_MULTIPART_MSG),
    }


if _MULTIPART:

    @app.post("/align")
    async def align(audio: UploadFile = File(...), text: str = Form(...)):
        path = await _save_upload(audio)
        try:
            sentences, duration = _align(path, text)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        return {
            "sentences": sentences,
            "srt": _build_srt(sentences),
            "duration": duration,
            "count": len(sentences),
        }

    @app.post("/srt")
    async def srt(audio: UploadFile = File(...), text: str = Form(...)):
        path = await _save_upload(audio)
        try:
            sentences, _ = _align(path, text)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        body = _build_srt(sentences)
        return Response(
            content=body.encode("utf-8"),
            media_type="application/x-subrip; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="narration.srt"'},
        )

else:
    # Routes still exist so callers get a clear 503 instead of a 404.
    @app.post("/align")
    @app.post("/srt")
    def _upload_routes_disabled():
        raise HTTPException(status_code=503, detail=_NO_MULTIPART_MSG)


def main():
    ap = argparse.ArgumentParser(description="Bookreel FunASR forced-alignment service")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--port", type=int, default=9101)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--model", default="fa-zh",
                    help="FunASR model id; fa-zh is the Chinese forced aligner")
    ap.add_argument("--cache", default=os.path.normpath(os.path.join(here, "..", "models", "funasr")),
                    help="where FunASR caches its downloaded weights")
    ap.add_argument("--preload", action="store_true", help="load at startup")
    args = ap.parse_args()

    if args.device == "cpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    CFG.update(device=args.device, model=args.model, cache=os.path.abspath(args.cache))
    os.environ.setdefault("MODELSCOPE_CACHE", CFG["cache"])

    print("[asr] model     : %s" % CFG["model"], flush=True)
    print("[asr] cache dir : %s" % CFG["cache"], flush=True)
    print("[asr] listening : http://%s:%d" % (args.host, args.port), flush=True)
    if _MULTIPART:
        print("[asr] POST /align with audio=<file> and text=<narration> to get an SRT", flush=True)
    else:
        print("[asr] ! %s" % _NO_MULTIPART_MSG, flush=True)
        print("[asr] ! serving /health only until then.", flush=True)

    if args.preload:
        _load()
        if _STATE["error"]:
            print("[asr] %s" % _STATE["error"], flush=True)
            sys.exit(1)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
