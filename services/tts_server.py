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
import gc
import io
import os
import sys
import tempfile
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

# Host RAM is the scarce side of this box: the BIOS gives the GPU 96GiB and
# Linux 31GiB, and ComfyUI holds ~24GiB of that while a clip renders. CosyVoice2
# resident on top pushed the machine 7GiB into swap and the kernel began
# killing processes; stopping tts and asr took swap from 7GiB back to 1GiB.
# Narration and video are different stages of the same run, so the model has no
# reason to sit there between them. Drop it after this many idle seconds and
# pay the ~11s reload once per narration batch. 0 disables.
IDLE_UNLOAD = float(os.environ.get("BOOKREEL_TTS_IDLE_UNLOAD", "300"))
_LAST_USE = [0.0]


def _voice_files(name):
    d = CFG["voices_dir"]
    wav = os.path.join(d, name + ".wav")
    txt = os.path.join(d, name + ".txt")
    return wav, txt


def _rhythm_of(name):
    """Which voice this one borrows its DELIVERY from, or "".

    Zero-shot copies the reference clip's prosody along with its timbre, so
    cadence and timbre normally arrive together and cannot be chosen
    separately: pace, stress and phrasing all come from whichever 3-10 seconds
    of speech is in voices/<name>.wav. Measured on one 36-character line, the
    same text ran 9.30 s off one reference and 11.82 s off another.

    An optional voices/<name>.rhythm file - one line, the name of another
    voice - splits the two. The line is spoken first in the RHYTHM voice and
    then converted to this one's timbre (CosyVoice's inference_vc), so the
    delivery is the rhythm voice's and the person is this one. It is a file
    rather than a request field so that a voice carries its own definition and
    every picker keeps working unchanged.

    One hop only: the rhythm voice's own .rhythm file is ignored, because a
    chain is a loop waiting to happen and two conversions of the same audio
    sound like neither voice.
    """
    p = os.path.join(CFG["voices_dir"], name + ".rhythm")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            src = fh.read().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return ""
    if not src or src == name:
        return ""
    w, t = _voice_files(src)
    return src if (os.path.isfile(w) and os.path.isfile(t)) else ""


def _prompt_text(text):
    """The reference clip's transcript in the form this generation expects.

    CosyVoice3 was trained with the prompt transcript behind a system line and
    every example in its own repo passes one; without it the model is being
    asked for a shape it did not see in training. CosyVoice2 has no such line
    and must not be given one -- it would be read as words to speak.
    """
    if _STATE.get("gen") == 3:
        return "You are a helpful assistant.<|endofprompt|>" + text
    return text


def _instruct_text(text):
    """A spoken direction in the form this generation expects.

    CosyVoice3's LLM asserts that <|endofprompt|> is in the text it is handed
    (llm.py: `assert 151646 in text`), and on the instruct2 path the direction
    IS that text - the reference transcript is not used. Without the marker
    every instructed line (an `inner` thought, any dialog with
    BOOKREEL_DIALOG_INSTRUCT on) came back HTTP 500 (2026-10-15, C01S06
    不好，失态了). The narration rule measured instruct2 obeying on v3 with
    exactly this suffix. v2 gets the direction untouched.
    """
    if _STATE.get("gen") == 3 and "<|endofprompt|>" not in text:
        return text + "<|endofprompt|>"
    return text


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


def _rss_gb():
    try:
        with open("/proc/self/statm", "r") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") / 1e9)
    except (OSError, IndexError, ValueError):
        return 0.0


# Above this many GB still held after the engine is dropped, re-exec instead of
# pretending the memory came back. ComfyUI needs ~24.5 GB of the 31 GiB host to
# render a segment, so anything above ~5 GB sitting here is the difference
# between a render and an OOM kill.
RSS_CEILING_GB = float(os.environ.get("BOOKREEL_TTS_RSS_CEILING", "3.0"))


def _reaper():
    """Release the engine after IDLE_UNLOAD seconds with no synthesis.

    Dropping the handle is not the same as giving the memory back. Python's
    allocator keeps the arenas: with CosyVoice2 that left 4.7 GB resident, and
    with the bigger v3 weights it is 6.1 GB - measured 2026-09-16, after which
    ComfyUI (24.5 GB) could no longer load and the kernel killed it THREE
    times in one day, each kill costing the shot in flight. malloc_trim gives
    back what it can; when that is not enough the process re-execs itself,
    which is the only thing that reliably returns the pages. That is safe here
    because the service holds no state - voices are read from disk per request
    - and it costs an ~11 s reload that the next call was going to pay anyway.
    """
    while True:
        time.sleep(30)
        if IDLE_UNLOAD <= 0:
            continue
        with _LOCK:
            if _STATE["engine"] is None:
                continue
            idle = time.time() - _LAST_USE[0]
            if idle < IDLE_UNLOAD:
                continue
            _STATE["engine"] = None
            print("[tts] idle %.0fs, released CosyVoice%d" % (idle, _STATE.get("gen", 2)),
                  flush=True)
        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:                                   # noqa: BLE001
            pass
        held = _rss_gb()
        if held > RSS_CEILING_GB:
            # Never re-exec out from under a synthesis. If one has started in
            # the meantime the lock is held, and this simply waits for the next
            # idle window rather than losing a shot's narration.
            if not _SYNTH_LOCK.acquire(blocking=False):
                continue
            print("[tts] %.1f GB still held after unload -- re-exec to give it back"
                  % held, flush=True)
            sys.stdout.flush()
            sys.stderr.flush()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        print("[tts] %.1f GB resident after unload" % held, flush=True)


def _load():
    """Import + load CosyVoice2 once, lazily."""
    _LAST_USE[0] = time.time()
    if _STATE["engine"] is not None or _STATE["error"] is not None:
        return
    with _LOCK:
        if _STATE["engine"] is not None or _STATE["error"] is not None:
            return

        # The CosyVoice repo is not a pip package -- install.bat clones it to
        # third_party/CosyVoice and we add it (plus its Matcha-TTS submodule)
        # to sys.path here.
        # CosyVoice3 first: that checkout's AutoModel loads a v2 model dir as
        # well as a v3 one (it picks on which of cosyvoice{,2,3}.yaml is in the
        # directory), so one checkout speaks both and the generation is chosen
        # by --model alone. The old checkout stays as the fallback, which is
        # what makes rolling back a path change rather than a code change.
        here = os.path.dirname(os.path.abspath(__file__))
        for cand in (
            os.path.join(here, "..", "third_party", "CosyVoice3"),
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

        loader = None
        try:
            from cosyvoice.cli.cosyvoice import AutoModel as loader
        except Exception:  # noqa: BLE001  - older checkout, no AutoModel
            try:
                from cosyvoice.cli.cosyvoice import CosyVoice2 as loader
            except Exception as exc:  # noqa: BLE001
                _STATE["error"] = (
                    "CosyVoice is not importable. It is not on PyPI -- run install.bat "
                    "step [2/4], which clones it into third_party\\CosyVoice. Detail: %s" % exc
                )
                return

        path = CFG["model_path"]
        if not path or not os.path.isdir(path):
            _STATE["error"] = (
                "model directory not found: %r -- run download.bat step [5/5] first" % path
            )
            return

        # Which generation this model dir is, read off the file the loader
        # itself dispatches on. It decides one thing downstream: v3 wants the
        # reference transcript behind a system line (see _prompt_text).
        gen = 3 if os.path.isfile(os.path.join(path, "cosyvoice3.yaml")) else 2
        _STATE["gen"] = gen

        t0 = time.time()
        print("[tts] loading CosyVoice%d from %s ..." % (gen, path), flush=True)
        try:
            # No load_jit: CosyVoice3.__init__ does not take it, and it is
            # False everywhere here anyway.
            engine = loader(model_dir=path, fp16=False)
        except TypeError:
            engine = loader(path)  # older checkout, positional only
        except Exception as exc:  # noqa: BLE001
            _STATE["error"] = "failed to load CosyVoice%d: %s" % (gen, exc)
            print("[tts] load failed: %s" % exc, flush=True)
            return
        _STATE["engine"] = engine
        _STATE["sr"] = int(getattr(engine, "sample_rate", 24000))
        print("[tts] ready in %.1fs, sr=%d, cosyvoice%d"
              % (time.time() - t0, _STATE["sr"], gen), flush=True)


def _prompt_forms(wav_path):
    """The reference clip in the order this checkout is likely to accept it.

    Yields at most two candidates; once one has worked _STATE remembers it and
    every later call goes straight to that one.
    """
    def tensor():
        from cosyvoice.utils.file_utils import load_wav
        return load_wav(wav_path, 16000)

    form = _STATE.get("prompt_form")
    if form == "path":
        return [wav_path]
    if form == "tensor":
        return [tensor()]
    return [wav_path, tensor()]


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
        # Read off the model directory, not hardcoded: this used to say
        # cosyvoice2 while a v3 directory was speaking. Answers before the
        # first load too, when _STATE has no generation yet.
        "engine": "cosyvoice%d" % (
            _STATE.get("gen")
            or (3 if os.path.isfile(os.path.join(CFG["model_path"], "cosyvoice3.yaml")) else 2)),
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

    speed = float(req.speed or 1.0)
    if not 0.5 <= speed <= 2.0:
        raise HTTPException(status_code=400, detail="speed must be between 0.5 and 2.0")

    rhythm = _rhythm_of(name)
    t0 = time.time()
    chunks = []
    engine = _STATE["engine"]
    with _SYNTH_LOCK:
        if rhythm:
            # Two passes. The first speaks the line in the rhythm voice, which
            # is what fixes the cadence; the second keeps that performance and
            # replaces the speaker. Speed belongs to the first pass - it is the
            # one generating the timing, and applying it twice stretches the
            # audio twice.
            rw, rt = _voice_files(rhythm)
            with open(rt, "r", encoding="utf-8") as fh:
                rtext = fh.read().strip()
            src = []
            try:
                for piece in engine.inference_zero_shot(text, _prompt_text(rtext), rw,
                                                        stream=False, speed=speed):
                    src.append(piece["tts_speech"])
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500,
                                    detail="rhythm pass failed (%s): %s" % (rhythm, exc))
            if not src:
                raise HTTPException(status_code=500, detail="rhythm pass produced no audio")
            import torch as _t
            mid = _t.cat(src, dim=-1) if len(src) > 1 else src[0]
            fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="bookreel-vc-")
            os.close(fd)
            try:
                with open(tmp, "wb") as fh:
                    fh.write(_to_wav_bytes(mid, _STATE["sr"]))
                for piece in engine.inference_vc(tmp, wav_path, stream=False, speed=1.0):
                    chunks.append(piece["tts_speech"])
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500,
                                    detail="voice conversion failed (%s -> %s): %s"
                                           % (rhythm, name, exc))
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            if not chunks:
                raise HTTPException(status_code=500, detail="voice conversion produced no audio")
        last = None
        # Which of the two CosyVoice APIs this checkout speaks. The current
        # repo wants the reference clip as a PATH and loads it itself, twice,
        # at 16k for the speaker embedding and 24k for the flow features.
        # Older revisions wanted a tensor the caller had already loaded at
        # 16k. Passing the wrong one dies in the frontend, before any
        # generation, so trying the path first and falling back costs nothing.
        for prompt_wav in ([] if chunks else _prompt_forms(wav_path)):
            chunks = []
            try:
                if req.instruct:
                    gen = engine.inference_instruct2(
                        text, _instruct_text(req.instruct), prompt_wav, stream=False, speed=speed
                    )
                else:
                    gen = engine.inference_zero_shot(
                        text, _prompt_text(prompt_text), prompt_wav, stream=False, speed=speed
                    )
                for piece in gen:
                    chunks.append(piece["tts_speech"])
                _STATE["prompt_form"] = "path" if prompt_wav is wav_path else "tensor"
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        else:
            if not chunks:
                raise HTTPException(status_code=500, detail="synthesis failed: %s" % last)

    _LAST_USE[0] = time.time()
    if not chunks:
        raise HTTPException(status_code=500, detail="model returned no audio")

    import torch

    audio = torch.cat(chunks, dim=-1) if len(chunks) > 1 else chunks[0]
    data = _to_wav_bytes(audio, _STATE["sr"])
    secs = len(data) / (2.0 * _STATE["sr"])
    took = time.time() - t0
    print("[tts] %s%s | %d chars -> %.1fs audio in %.1fs (%.1fx realtime)"
          % (name, (" (rhythm %s)" % rhythm) if rhythm else "",
             len(text), secs, took, (took / secs) if secs else 0), flush=True)

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



def _resolve_device(name):
    """Return a device torch can actually use, falling back to CPU.

    "mps" is Apple Silicon's Metal backend; the stock macOS torch wheel has
    it, but a model can still hit an op Metal has no kernel for, so turn on
    the CPU fallback for those ops instead of crashing. Set the env var here
    as well as in setup.command so a hand-started server behaves the same.
    """
    if name != "mps":
        return name
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    try:
        import torch
    except Exception:
        print("%s torch not importable -- using cpu" % "[tts]", flush=True)
        return "cpu"
    mps = getattr(torch.backends, "mps", None)
    if mps is None or not mps.is_available():
        print("%s mps unavailable (not Apple Silicon, or a CPU-only torch)"
              " -- using cpu" % "[tts]", flush=True)
        return "cpu"
    return "mps"

def main():
    ap = argparse.ArgumentParser(description="Bookreel CosyVoice2 narration service")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--port", type=int, default=9100)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=os.path.normpath(os.path.join(here, "..", "models", "cosyvoice2")))
    ap.add_argument("--voices", default=os.path.normpath(os.path.join(here, "..", "voices")))
    ap.add_argument("--out", default=os.path.normpath(os.path.join(here, "..", "output", "audio")))
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda", "mps"])
    ap.add_argument("--no-save", action="store_true", help="do not write wavs to the output dir")
    ap.add_argument("--preload", action="store_true", help="load weights at startup")
    args = ap.parse_args()

    if args.device != "cuda":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    CFG.update(
        model_path=os.path.abspath(args.model),
        voices_dir=os.path.abspath(args.voices),
        out_dir=os.path.abspath(args.out),
        device=_resolve_device(args.device),
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

    if IDLE_UNLOAD > 0:
        threading.Thread(target=_reaper, name="tts-idle-unload", daemon=True).start()
        print("[tts] releases the model after %.0fs idle" % IDLE_UNLOAD, flush=True)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
