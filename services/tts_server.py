# -*- coding: utf-8 -*-
"""Bookreel · CosyVoice2 narration service (slot: 旁白).

Speaks the contract the web console already uses:

    POST /tts  {"text": "...", "voice": "storyteller", "speed": 0.95, "format": "wav"}

CosyVoice2-0.5B has no built-in speaker table -- every synthesis is zero-shot
off a reference clip. So each voice is a pair of files under voices/:

    voices/storyteller.wav   3-10s of clean speech, mono, 16kHz+
    voices/storyteller.txt   the exact transcript of that clip

Add a voice by dropping in another pair; the name is the filename.

    python tts_server.py --model ..\\models\\cosyvoice2 --port 9100 --device cpu

Endpoints
    GET  /health   -> readiness, loaded flag, available voices
    GET  /voices   -> list of usable voices with their prompt transcripts
    POST /tts      -> audio bytes (wav), or {"error": ...} on failure
"""

import argparse
import io
import os
import sys
import threading
import time
import wave

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Bookreel TTS (CosyVoice2)", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CFG = {"model_path": "", "voices_dir": "", "device": "cpu", "out_dir": "", "save": True}
_STATE = {"engine": None, "sr": 24000, "error": None}
_LOCK = threading.Lock()
_SYNTH_LOCK = threading.Lock()  # CosyVoice is not thread-safe; serialize requests


def _voice_files(name):
    d = CFG["voices_dir"]
    wav = os.path.join(d, name + ".wav")
    txt = os.path.join(d, name + ".txt")
    return wav, txt


def _list_voices():
    d = CFG["voices_dir"]
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".wav"):
            continue
        name = fn[:-4]
        wav, txt = _voice_files(name)
        if os.path.isfile(txt):
            try:
                with open(txt, "r", encoding="utf-8") as fh:
                    prompt = fh.read().strip()
            except OSError:
                prompt = ""
            out.append({"name": name, "prompt_text": prompt, "wav": wav})
    return out


def _load():
    """Import + load CosyVoice2 once, lazily."""
    if _STATE["engine"] is not None or _STATE["error"] is not None:
        return
    with _LOCK:
        if _STATE["engine"] is not None or _STATE["error"] is not None:
            return

        # The CosyVoice repo is not a pip package -- install.bat clones it to
        # third_party/CosyVoice and we add it (plus its Matcha-TTS submodule)
        # to sys.path here.
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (
            os.path.join(here, "..", "third_party", "CosyVoice"),
            os.path.join(here, "..", "CosyVoice"),
        ):
            cand = os.path.normpath(cand)
            if os.path.isdir(cand):
                if cand not in sys.path:
                    sys.path.insert(0, cand)
                matcha = os.path.join(cand, "third_party", "Matcha-TTS")
                if os.path.isdir(matcha) and matcha not in sys.path:
                    sys.path.insert(0, matcha)
                break

        try:
            from cosyvoice.cli.cosyvoice import CosyVoice2
        except Exception as exc:  # noqa: BLE001
            _STATE["error"] = (
                "CosyVoice2 is not importable. It is not on PyPI -- run install.bat "
                "step [2/4], which clones it into third_party\\CosyVoice. Detail: %s" % exc
            )
            return

        path = CFG["model_path"]
        if not path or not os.path.isdir(path):
            _STATE["error"] = (
                "model directory not found: %r -- run download.bat step [5/5] first" % path
            )
            return

        t0 = time.time()
        print("[tts] loading CosyVoice2 from %s ..." % path, flush=True)
        try:
            engine = CosyVoice2(path, load_jit=False, load_trt=False, fp16=False)
        except Exception as exc:  # noqa: BLE001
            _STATE["error"] = "failed to load CosyVoice2: %s" % exc
            print("[tts] load failed: %s" % exc, flush=True)
            return
        _STATE["engine"] = engine
        _STATE["sr"] = int(getattr(engine, "sample_rate", 24000))
        print("[tts] ready in %.1fs, sr=%d" % (time.time() - t0, _STATE["sr"]), flush=True)


def _to_wav_bytes(tensor, sr):
    """Serialize a float torch tensor in [-1,1] to 16-bit PCM wav bytes."""
    import numpy as np

    arr = tensor.detach().cpu().numpy() if hasattr(tensor, "detach") else np.asarray(tensor)
    arr = arr.reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = "storyteller"
    speed: Optional[float] = 1.0
    format: Optional[str] = "wav"
    instruct: Optional[str] = None   # e.g. "用低沉平缓的语气讲述"
    save_as: Optional[str] = None    # filename inside the output dir


@app.get("/health")
def health():
    voices = _list_voices()
    return {
        "status": "ok",
        "engine": "cosyvoice2",
        "path": CFG["model_path"],
        "device": CFG["device"],
        "sample_rate": _STATE["sr"],
        "loaded": _STATE["engine"] is not None,
        "voices": [v["name"] for v in voices],
        "error": _STATE["error"],
    }


@app.get("/voices")
def voices():
    return {"voices": [{"name": v["name"], "prompt_text": v["prompt_text"]} for v in _list_voices()]}


@app.post("/tts")
def tts(req: TTSRequest):
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is empty")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="text too long (max 2000 chars per call)")
    if (req.format or "wav").lower() != "wav":
        raise HTTPException(status_code=400, detail="only format=wav is supported")

    _load()
    if _STATE["error"]:
        raise HTTPException(status_code=503, detail=_STATE["error"])

    name = req.voice or "storyteller"
    wav_path, txt_path = _voice_files(name)
    if not (os.path.isfile(wav_path) and os.path.isfile(txt_path)):
        avail = [v["name"] for v in _list_voices()]
        raise HTTPException(
            status_code=404,
            detail=(
                "voice %r needs both %s and %s. CosyVoice2 is zero-shot only -- "
                "drop in a 3-10s reference clip plus its exact transcript. Available: %s"
                % (name, os.path.basename(wav_path), os.path.basename(txt_path), avail or "none")
            ),
        )

    with open(txt_path, "r", encoding="utf-8") as fh:
        prompt_text = fh.read().strip()

    from cosyvoice.utils.file_utils import load_wav

    prompt_speech = load_wav(wav_path, 16000)
    speed = float(req.speed or 1.0)
    if not 0.5 <= speed <= 2.0:
        raise HTTPException(status_code=400, detail="speed must be between 0.5 and 2.0")

    t0 = time.time()
    chunks = []
    engine = _STATE["engine"]
    with _SYNTH_LOCK:
        try:
            if req.instruct:
                gen = engine.inference_instruct2(
                    text, req.instruct, prompt_speech, stream=False, speed=speed
                )
            else:
                gen = engine.inference_zero_shot(
                    text, prompt_text, prompt_speech, stream=False, speed=speed
                )
            for piece in gen:
                chunks.append(piece["tts_speech"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="synthesis failed: %s" % exc)

    if not chunks:
        raise HTTPException(status_code=500, detail="model returned no audio")

    import torch

    audio = torch.cat(chunks, dim=-1) if len(chunks) > 1 else chunks[0]
    data = _to_wav_bytes(audio, _STATE["sr"])
    secs = len(data) / (2.0 * _STATE["sr"])
    took = time.time() - t0
    print("[tts] %s | %d chars -> %.1fs audio in %.1fs (%.1fx realtime)"
          % (name, len(text), secs, took, (took / secs) if secs else 0), flush=True)

    headers = {"X-Audio-Seconds": "%.2f" % secs, "X-Synth-Seconds": "%.2f" % took}

    if CFG["save"] and CFG["out_dir"]:
        try:
            os.makedirs(CFG["out_dir"], exist_ok=True)
            fn = req.save_as or ("narration-%d.wav" % int(time.time() * 1000))
            fn = os.path.basename(fn)
            dest = os.path.join(CFG["out_dir"], fn)
            with open(dest, "wb") as fh:
                fh.write(data)
            headers["X-Saved-As"] = fn
        except OSError as exc:
            print("[tts] could not save: %s" % exc, flush=True)

    return Response(content=data, media_type="audio/wav", headers=headers)


def main():
    ap = argparse.ArgumentParser(description="Bookreel CosyVoice2 narration service")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=os.path.normpath(os.path.join(here, "..", "models", "cosyvoice2")))
    ap.add_argument("--voices", default=os.path.normpath(os.path.join(here, "..", "voices")))
    ap.add_argument("--out", default=os.path.normpath(os.path.join(here, "..", "output", "audio")))
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--no-save", action="store_true", help="do not write wavs to the output dir")
    ap.add_argument("--preload", action="store_true", help="load weights at startup")
    args = ap.parse_args()

    if args.device == "cpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    CFG.update(
        model_path=os.path.abspath(args.model),
        voices_dir=os.path.abspath(args.voices),
        out_dir=os.path.abspath(args.out),
        device=args.device,
        save=not args.no_save,
    )

    print("[tts] model dir : %s" % CFG["model_path"], flush=True)
    print("[tts] voices    : %s" % CFG["voices_dir"], flush=True)
    found = [v["name"] for v in _list_voices()]
    if found:
        print("[tts] voices found: %s" % ", ".join(found), flush=True)
    else:
        print("[tts] NOTE no voices yet. CosyVoice2 is zero-shot only -- add", flush=True)
        print("[tts]      voices\\storyteller.wav (3-10s clip) + storyteller.txt", flush=True)
        print("[tts]      (its exact transcript) before calling /tts.", flush=True)
    print("[tts] listening : http://%s:%d" % (args.host, args.port), flush=True)

    if args.preload:
        _load()
        if _STATE["error"]:
            print("[tts] %s" % _STATE["error"], flush=True)
            sys.exit(1)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
