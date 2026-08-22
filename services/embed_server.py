# -*- coding: utf-8 -*-
"""Bookreel · CPU embedding service (slot: retrieval).

OpenAI-compatible embeddings so anything that speaks /v1/embeddings can talk
to it. Runs on CPU by design -- the GPU is reserved for the LLM / Flux / Hailuo
slots, and Qwen3-Embedding-0.6B is small enough that CPU is fine.

    python embed_server.py --port 8002 --model ..\\models\\qwen3-embedding

Endpoints
    GET  /health          -> {"status":"ok","model":...,"dim":...,"loaded":bool}
    GET  /v1/models       -> OpenAI-style model list
    POST /v1/embeddings   -> {"input": str | [str], "model": str}
"""

import argparse
import os
import sys
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Union, Optional

app = FastAPI(title="Bookreel Embeddings", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

CFG = {"model_path": "", "device": "cpu", "name": "qwen3-embedding", "batch": 16}
_STATE = {"model": None, "dim": None, "error": None}
_LOCK = threading.Lock()


def _load():
    """Load lazily and only once. Keeps startup instant so /health answers
    immediately even while the weights are still coming off disk."""
    if _STATE["model"] is not None or _STATE["error"] is not None:
        return
    with _LOCK:
        if _STATE["model"] is not None or _STATE["error"] is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            _STATE["error"] = (
                "sentence-transformers is not installed in this venv. "
                "Run install.bat step [3/4] (venv-tools). Detail: %s" % exc
            )
            return
        path = CFG["model_path"]
        if not path or not os.path.isdir(path):
            _STATE["error"] = (
                "model directory not found: %r -- run download.bat step [3/5] first" % path
            )
            return
        t0 = time.time()
        print("[embed] loading %s on %s ..." % (path, CFG["device"]), flush=True)
        try:
            model = SentenceTransformer(path, device=CFG["device"])
            dim = int(model.get_sentence_embedding_dimension())
        except Exception as exc:  # noqa: BLE001 - surface anything to the client
            _STATE["error"] = "failed to load model: %s" % exc
            print("[embed] load failed: %s" % exc, flush=True)
            return
        _STATE["model"] = model
        _STATE["dim"] = dim
        print("[embed] ready in %.1fs, dim=%d" % (time.time() - t0, dim), flush=True)


class EmbedRequest(BaseModel):
    input: Union[str, List[str]]
    model: Optional[str] = None
    encoding_format: Optional[str] = "float"


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": CFG["name"],
        "path": CFG["model_path"],
        "device": CFG["device"],
        "dim": _STATE["dim"],
        "loaded": _STATE["model"] is not None,
        "error": _STATE["error"],
    }


@app.get("/v1/models")
def models():
    return {
        "object": "list",
        "data": [{"id": CFG["name"], "object": "model", "owned_by": "bookreel"}],
    }


@app.post("/v1/embeddings")
def embeddings(req: EmbedRequest):
    _load()
    if _STATE["error"]:
        raise HTTPException(status_code=503, detail=_STATE["error"])

    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    texts = [t for t in texts if t is not None]
    if not texts:
        raise HTTPException(status_code=400, detail="input is empty")
    if len(texts) > 512:
        raise HTTPException(status_code=400, detail="max 512 inputs per request")

    vectors = _STATE["model"].encode(
        texts,
        batch_size=CFG["batch"],
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )

    data = [
        {"object": "embedding", "index": i, "embedding": [float(x) for x in vec]}
        for i, vec in enumerate(vectors)
    ]
    # Rough token estimate; CJK runs about 1 token per character.
    approx = sum(len(t) for t in texts)
    return {
        "object": "list",
        "data": data,
        "model": req.model or CFG["name"],
        "usage": {"prompt_tokens": approx, "total_tokens": approx},
    }


def main():
    ap = argparse.ArgumentParser(description="Bookreel CPU embedding service")
    here = os.path.dirname(os.path.abspath(__file__))
    default_model = os.path.join(here, "..", "models", "qwen3-embedding")
    ap.add_argument("--port", type=int, default=8002)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--model", default=os.path.normpath(default_model))
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--name", default="qwen3-embedding")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--preload", action="store_true",
                    help="load weights at startup instead of on first request")
    args = ap.parse_args()

    CFG.update(model_path=os.path.abspath(args.model), device=args.device,
               name=args.name, batch=args.batch)

    print("[embed] model dir : %s" % CFG["model_path"], flush=True)
    print("[embed] listening : http://%s:%d" % (args.host, args.port), flush=True)
    if args.preload:
        _load()
        if _STATE["error"]:
            print("[embed] %s" % _STATE["error"], flush=True)
            sys.exit(1)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
