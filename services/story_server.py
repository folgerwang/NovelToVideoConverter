# -*- coding: utf-8 -*-
"""Bookreel . step 1 of 3: the story engine (slot A only -- no image/video models).

This is the LLM-only half of the pipeline. It never touches ComfyUI, so it can
run with every other model unloaded, which on unified-memory hardware is the
whole point: one slot owns the GPU at a time and VRAM is released by stopping
the previous slot's process (ComfyUI's /free does NOT give it back).

The flow it implements:

    1. blueprint      premise -> a production bible: world, era, culture,
                      language, visual style, cast, locations, props, and a
                      chapter outline. Written ONCE and then frozen, because
                      everything downstream quotes it for consistency.
    2. revise         the user reads the blueprint and says what to change.
                      Iterate here -- it is far cheaper than discovering the
                      world is wrong after forty video clips.
    3. chapter N      prose for one chapter, given the blueprint plus the
                      summaries of every earlier chapter (continuity).
    4. script N       that chapter as a director's script: shots with dialog,
                      expression, location, time, camera setup / cut / movement.
    5. assets         image prompts for every character, location and prop,
                      phrased in the blueprint's own world terms, with a locked
                      seed each so step 2 can regenerate any single asset
                      without the rest drifting.

Everything lands on disk under projects/<slug>/ as plain JSON, so step 2
(images) and step 3 (video) read files rather than needing this service up.

    python story_server.py --port 8010 --llm http://127.0.0.1:11434/v1

Endpoints
    GET  /health
    GET  /projects                  -> list of slugs
    GET  /project/{slug}            -> everything saved for one project
    POST /blueprint                 -> create/overwrite a project blueprint
    POST /blueprint/revise          -> apply the user's notes to the blueprint
    POST /chapter                   -> write chapter n
    POST /script                    -> director's script for chapter n
    POST /assets                    -> the image-prompt library
"""

import argparse
import collections
import json
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Any, Dict, List, Optional

app = FastAPI(title="Bookreel story engine", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

CFG = {
    "llm": "http://127.0.0.1:11434/v1",
    "model": "qwen3.8-27b",
    "projects": "",
    "timeout": 1800,
    "max_tokens": 8192,
}


# ----------------------------------------------------------------------
# the terminal, mirrored to the browser
# ----------------------------------------------------------------------
# Everything this process prints -- our own [story] lines, uvicorn's warnings,
# and any traceback -- is teed into a ring buffer the UI polls. The point is
# not prettiness: when a ten-minute call is running, the only honest evidence
# that it is working rather than wedged is the server's own output.
_LOG = collections.deque(maxlen=800)
_LOG_SEQ = [0]
_LOGLOCK = threading.Lock()


class _Tee(object):
    """Writes through to the real stream and keeps whole lines for /log."""

    def __init__(self, stream, kind):
        self._s = stream
        self._kind = kind
        self._buf = ""

    def write(self, text):
        try:
            self._s.write(text)
        except Exception:                              # noqa: BLE001
            pass
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                with _LOGLOCK:
                    _LOG_SEQ[0] += 1
                    _LOG.append({"seq": _LOG_SEQ[0], "t": time.strftime("%H:%M:%S"),
                                 "kind": self._kind, "text": line.rstrip()})
        return len(text)

    def flush(self):
        try:
            self._s.flush()
        except Exception:                              # noqa: BLE001
            pass

    def isatty(self):
        return False


@app.get("/log")
def get_log(since: int = 0, limit: int = 200):
    """Incremental tail. The client passes back the `next` it last received,
    so a reconnecting page does not replay the whole buffer."""
    with _LOGLOCK:
        rows = [r for r in _LOG if r["seq"] > since][-max(1, min(limit, 500)):]
        nxt = _LOG_SEQ[0]
    return {"lines": rows, "next": nxt}


# ----------------------------------------------------------------------
# progress, so the UI can say something truthful during a 10-minute call
# ----------------------------------------------------------------------
# There is no token-level progress to report: the OpenAI-compatible call is a
# single blocking request and the model streams nothing back we subscribe to.
# What we CAN report honestly is which stage is running, how long it has been
# running, and whether we are on a JSON repair retry -- that last one is the
# difference between "slow" and "stuck", and it is the question a user actually
# has when a bar sits still for eight minutes.
# Progress is tracked PER JOB, not in one global. Two calls can overlap -- a
# batch chapter still finishing while the user starts a blueprint -- and with a
# single shared dict the one that finishes first wipes the other's state, so the
# survivor's bar goes blank while it is still working. Each request thread owns
# a job id; /progress reports the newest live job.
_JOBS = {}
_JOB_SEQ = [0]
_LAST = [""]
_PLOCK = threading.Lock()
_local = threading.local()

_EXPECT = {"蓝图": 3200, "修改蓝图": 900, "正文": 2600, "分镜": 3600, "资产提示词": 1600}
_EXPECT_MIN = 200


def _expect_for(op):
    return max(_EXPECT_MIN, int(_EXPECT.get(op, 2000)))


def _learn(op, tokens):
    """Exponential moving average. One outlier should nudge the estimate, not
    redefine it."""
    if tokens < _EXPECT_MIN:
        return
    prev = _EXPECT.get(op)
    _EXPECT[op] = int(tokens if prev is None else (prev * 0.7 + tokens * 0.3))


def _prog(**kw):
    """Update this thread's job only. A job that has already ended is ignored,
    so a late streaming callback cannot resurrect a finished bar."""
    tok = getattr(_local, "token", None)
    if tok is None:
        return
    with _PLOCK:
        job = _JOBS.get(tok)
        if job is not None:
            job.update(kw)


def _current_op():
    tok = getattr(_local, "token", None)
    with _PLOCK:
        job = _JOBS.get(tok) or {}
    return job.get("op", "")


def _prog_begin(op, detail="", step=0, steps=0):
    with _PLOCK:
        _JOB_SEQ[0] += 1
        tok = _JOB_SEQ[0]
        _JOBS[tok] = {"id": tok, "active": True, "op": op, "detail": detail,
                      "started": time.time(), "step": step, "steps": steps,
                      "attempt": 0, "tries": 0, "tokens": 0,
                      "expect": _expect_for(op), "pct": 0, "tps": 0.0}
    _local.token = tok
    print("[story] > %s %s ... (模型思考中，通常数分钟)" % (op, detail), flush=True)
    return tok


def _prog_end(summary=""):
    tok = getattr(_local, "token", None)
    with _PLOCK:
        _JOBS.pop(tok, None)
        if summary:
            _LAST[0] = summary
    _local.token = None
    if summary:
        print("[story] < %s" % summary, flush=True)


@app.get("/progress")
def progress():
    """Every live job, plus the newest one flattened onto the top level.

    `running` alone was not enough. Two generations really do overlap -- a
    reloaded page fired a second one while the first was still going -- and the
    console could only draw the newest, so the other was invisible while it sat
    in the queue burning wall-clock. `jobs` carries them all, newest last; the
    top-level fields stay where they were so nothing that reads them breaks.
    """
    with _PLOCK:
        live = sorted(_JOBS.values(), key=lambda j: j["started"])
        last = _LAST[0]
    now = time.time()
    jobs = []
    for j in live:
        row = dict(j)
        row["elapsed"] = int(now - row["started"])
        # Queued behind another job on the same GPU: alive, but not yet given
        # any tokens. Saying so beats a bar that sits at 0% looking stuck.
        row["waiting"] = row.get("tokens", 0) == 0 and row["elapsed"] > 30
        jobs.append(row)
    if not live:
        return {"active": False, "op": "", "detail": "", "elapsed": 0,
                "step": 0, "steps": 0, "attempt": 0, "tries": 0,
                "tokens": 0, "expect": 0, "pct": None, "tps": 0.0,
                "running": 0, "last": last, "jobs": []}
    p = dict(jobs[-1])
    p["running"] = len(live)
    p["last"] = last
    p["jobs"] = jobs
    return p


# ----------------------------------------------------------------------
# talking to slot A
# ----------------------------------------------------------------------
def _chat(system, user, effort="medium", max_tokens=None):
    """One OpenAI-compatible completion, streamed.

    Streaming is not for latency -- nothing renders the partial text. It is the
    only way to get a HONEST progress number: without it the call is an opaque
    ten-minute POST and the bar can only animate a stripe that means nothing.
    Counting chunks as they arrive gives a real numerator; _EXPECT supplies a
    denominator that calibrates itself from previous runs.

    Reasoning deltas are counted too. Qwen3 spends much of a high-effort call
    thinking, and a bar that ignores that would sit at zero for minutes and then
    leap -- which is exactly the behaviour that makes progress bars distrusted.
    """
    body = {
        "model": CFG["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.8, "top_p": 0.9,
        "max_tokens": max_tokens or CFG["max_tokens"],
        # Qwen3 thinks by default; for structured extraction that burns the
        # token budget before any JSON appears. Only the creative passes get it.
        "chat_template_kwargs": {"enable_thinking": effort == "high"},
        "stream": True,
    }
    req = urllib.request.Request(CFG["llm"].rstrip("/") + "/chat/completions",
                                 json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    parts, n, t0 = [], 0, time.time()
    op = _current_op()
    expect = _expect_for(op)
    try:
        with urllib.request.urlopen(req, timeout=CFG["timeout"]) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue
                choices = chunk.get("choices") or [{}]
                delta = choices[0].get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    parts.append(piece)
                # count thinking as work, because it is
                if piece or delta.get("reasoning") or delta.get("reasoning_content"):
                    n += 1
                    if n % 8 == 0:                     # ~every 8 tokens
                        el = max(0.001, time.time() - t0)
                        _prog(tokens=n, expect=expect,
                              pct=min(96, int(n * 100.0 / max(1, expect))),
                              tps=round(n / el, 1))
    except urllib.error.HTTPError as exc:
        raise HTTPException(status_code=502,
                            detail="LLM HTTP %s: %s" % (exc.code, exc.read()[:300]))
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=502, detail="LLM unreachable: %s" % exc)
    if n:
        _learn(op, n)
    _prog(tokens=n, pct=min(99, int(n * 100.0 / max(1, expect))))
    return "".join(parts).strip()


_THINK = re.compile(r"<think>[\s\S]*?</think>", re.I)


def _parse_json(text):
    """Pull the JSON object out of a model reply.

    Same shape as the console's parseJSON: strip <think>, prefer the last
    fenced block, then take the outermost braces. Models put prose around JSON
    no matter how firmly you ask them not to.
    """
    t = _THINK.sub("", text or "").strip()
    t = re.sub(r"```json", "```", t, flags=re.I)
    parts = [p for p in t.split("```") if p.strip()]
    if parts:
        t = parts[-1]
    a, b = t.find("{"), t.find("[")
    start = b if a < 0 else (a if b < 0 else min(a, b))
    if start < 0:
        raise ValueError("no JSON in reply: %s" % t[:200])
    end = max(t.rfind("}"), t.rfind("]"))
    return json.loads(t[start:end + 1])


_REPAIRS = (
    # trailing comma before a closer:  {"a":1,}   ->  {"a":1}
    (re.compile(r",(\s*[}\]])"), r"\1"),
    # missing comma between two objects/arrays across a line break
    (re.compile(r"([}\]\"])(\s*\n\s*)([{\[\"])"), r"\1,\2\3"),
    # missing comma after a bare number/true/false/null before the next key
    (re.compile(r"([0-9eE\.]|true|false|null)(\s*\n\s*\")"), r"\1,\2"),
)


def _fix_bracket_mismatch(text):
    """Close a container with the bracket it was actually opened with.

    Seen in a 37k-character shot list: `dialog` is an array, and the model
    closed it with `}` instead of `]`. The document is complete and every other
    character is right, so nothing else in the repair chain touches it -- the
    parser just reports a missing comma a thousand lines in. Walking the stack
    and swapping the closer costs nothing and rescues the whole reply.

    Returns None when every bracket already matches, so the chain does not
    offer a candidate identical to its input.
    """
    out, stack, in_str, esc, changed = [], [], False, False, False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            want = stack.pop() if stack else None
            if want and ch != want:
                out.append(want)
                changed = True
                continue
        out.append(ch)
    return "".join(out) if changed else None


def _escape_inner_quotes(text):
    """Escape ASCII quotes the model used as Chinese quotation marks.

    Qwen writes 半文半白（"兹有……"）straight into a JSON string value without
    escaping. That closes the string early and the parser complains about a
    missing comma a few characters later -- nothing to do with commas, and
    nothing the truncation repair can help with, because the document is whole.

    A quote that genuinely ends a string is followed, after whitespace, by one
    of : , } ] or by nothing at all. Any other quote is punctuation that
    belongs inside the string, so escape it and stay in.
    """
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            i += 1
        elif ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2])          # already escaped, leave alone
            i += 2
        elif ch == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ":,}]":
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _close_truncated(text):
    """Salvage a reply that simply stopped: reopen nothing, close everything.

    Walks the text once, tracking string/escape state and the stack of open
    brackets, and remembers every point where the document was *between*
    elements -- just before a comma, or just after a container closed. The last
    such point is a clean cut: everything before it is whole. Truncating there
    and appending the still-open closers turns a half-written object into a
    valid one that is short a few entries, which is worth far more than another
    eight-minute regeneration.

    Two candidates come back, best first: one cut where a container had just
    closed (whole elements only), then one that also cuts before a comma (keeps
    more, but the last element may be missing fields). A half-written shot with
    no description is worth less than no shot at all, so the tidy cut is tried
    first. Empty list when nothing was ever completed.
    """
    stack, in_str, esc = [], False, False
    whole = None          # just after a container closed -- no partial element
    any_ = None           # also just before a comma -- keeps more, may be partial
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack:
                stack.pop()
            whole = any_ = (i + 1, tuple(stack))
        elif ch == ",":
            any_ = (i, tuple(stack))
    out = []
    # Truncation lands inside a long string value far more often than between
    # elements, because that is where the tokens go. Cutting back to the last
    # element boundary then throws that value away -- which turned a chapter
    # holding 1700 characters of prose into {"n":6,"title":"断"}. Closing the
    # string keeps what was written, so offer that first.
    if in_str:
        head = text[:-1] if text.endswith("\\") else text
        out.append(head + '"' + "".join(reversed(stack)))
    for cand in (whole, any_):
        if not cand:
            continue
        cut, still_open = cand
        fixed = text[:cut] + "".join(reversed(still_open))
        if fixed not in out:
            out.append(fixed)
    return out


def _repair_json(text):
    """Try to fix the JSON a 27B actually emits, before paying for a rerun.

    The failure is almost always a missing or extra comma somewhere past ten
    thousand characters -- the model loses track of the structure, not of the
    content. Regenerating costs eight minutes; a comma costs nothing. Repairs
    are applied cumulatively and each stage is parsed, so the loosest rule is
    only reached if the stricter ones did not already succeed.
    """
    yield text
    cur = text
    for pattern, repl in _REPAIRS:
        fixed = pattern.sub(repl, cur)
        if fixed != cur:
            cur = fixed
            yield cur
    # A container closed with the wrong bracket type. Applied before the rest
    # because it leaves everything else untouched.
    fixed = _fix_bracket_mismatch(cur)
    if fixed:
        cur = fixed
        yield cur

    # Unescaped Chinese quotation marks inside a value. Applied cumulatively so
    # the truncation repair below works on the corrected text.
    quoted = _escape_inner_quotes(cur)
    if quoted != cur:
        cur = quoted
        yield cur

    # The common failure is not a stray comma at all: the model ran out of
    # token budget mid-object, so depth never returns to zero and the balanced
    # -brace scan below finds nothing. That is precisely why "自动修复无效"
    # kept printing. Cut back to the last element boundary and close what is
    # still open.
    for closed in _close_truncated(cur):
        if closed != cur:
            yield closed

    # trailing garbage after an already-complete object
    depth, last_ok, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(cur):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                last_ok = i
    if last_ok > 0 and last_ok < len(cur) - 1:
        yield cur[:last_ok + 1]


def _dump_raw(raw, why):
    """Park an unparseable reply next to the projects tree. Best effort: a
    diagnostic that can itself fail the request would be worse than none."""
    try:
        d = os.path.join(CFG["projects"] or ".", "_failed")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S-") +
                            re.sub(r"[^\w]+", "_", _current_op() or "call")[:24] + ".txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# %s\n# %s\n\n%s" % (time.strftime("%F %T"), why, raw))
        print("[story]   原文已存：%s" % path, flush=True)
    except Exception:                                  # noqa: BLE001
        pass


def _chat_json(system, user, effort="medium", tries=3, max_tokens=None, require=None):
    """_chat plus a repair loop. A 27B at q4 emits a trailing comma or an
    unquoted key often enough that retrying is cheaper than hand-parsing.

    `require` is what makes the repair safe. A salvaged document can be valid
    JSON and still be useless -- a truncated chapter repaired into
    {"n":6,"title":"断"} parses perfectly and contains no chapter, and was
    saved as a finished chapter of 0 characters. Callers pass a predicate for
    the field they actually came for; a candidate that fails it counts as a
    failure, not as a rescue.
    """
    def ok(obj):
        if require is None:
            return True
        try:
            return bool(require(obj))
        except Exception:                              # noqa: BLE001
            return False
    last = None
    for attempt in range(tries):
        _prog(attempt=attempt + 1, tries=tries)
        raw = _chat(system, user if attempt == 0 else
                    user + "\n\n上一次的回复不是合法 JSON（%s）。只输出 JSON，不要解释。" % last,
                    effort=effort, max_tokens=max_tokens)
        try:
            got = _parse_json(raw)
            if ok(got):
                return got
            raise ValueError("解析成功但缺少必要内容")
        except Exception as exc:                       # noqa: BLE001
            last = str(exc)[:160]
            print("[story] ! JSON parse failed (try %d/%d): %s" % (attempt + 1, tries, last),
                  flush=True)
            # Keep the reply. Without it the only evidence is a character
            # offset, and telling a truncated document from a malformed one
            # becomes guesswork -- which is how this bug survived so long.
            print("[story]   原文 %d 字，结尾：%s"
                  % (len(raw), repr(raw[-90:])), flush=True)
            _dump_raw(raw, last)
            # Before spending another eight minutes, try to fix the comma.
            for n_fix, candidate in enumerate(_repair_json(raw)):
                if n_fix == 0:
                    continue
                try:
                    out = _parse_json(candidate)
                    if not ok(out):
                        continue          # valid JSON, but the content is gone
                    print("[story] + JSON 自动修复成功（修复 %d）—— 原文不完整，"
                          "救回的内容可能少于模型本来要写的" % n_fix, flush=True)
                    return out
                except Exception:                      # noqa: BLE001
                    continue
            print("[story]   自动修复无效，重新生成", flush=True)
    print("[story] x gave up after %d tries -- the model never emitted valid JSON" % tries,
          flush=True)
    raise HTTPException(status_code=502, detail="model never returned valid JSON: %s" % last)


# ----------------------------------------------------------------------
# project storage -- plain JSON on disk, so steps 2 and 3 need no service
# ----------------------------------------------------------------------
def _slug(name):
    s = unicodedata.normalize("NFKC", str(name or "")).strip().lower()
    s = re.sub(r"[^\w一-鿿-]+", "-", s).strip("-")
    return s[:60] or "untitled"


def _proj_dir(slug, *rest):
    return os.path.join(CFG["projects"], _slug(slug), *rest)


def _save(slug, relpath, obj):
    path = _proj_dir(slug, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)                      # never leave a half-written file
    return path


def _load(slug, relpath, default=None):
    path = _proj_dir(slug, relpath)
    if not os.path.isfile(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _need_blueprint(slug):
    bp = _load(slug, "blueprint.json")
    if not bp:
        raise HTTPException(status_code=404,
                            detail="no blueprint for '%s' -- POST /blueprint first" % slug)
    return bp


def _world_brief(bp):
    """The paragraph every downstream prompt is anchored to.

    Steps 2 and 3 must not re-invent the world, so each call gets the same
    condensed statement of it rather than the whole blueprint (which would eat
    the context window once the cast is large)."""
    w = bp.get("world", {}) or {}
    v = bp.get("visual_style", {}) or {}
    return (
        "作品：《%s》（%s）\n世界：%s%s · %s · %s\n地理：%s\n历史局势：%s\n人物族裔：%s\n"
        "文化与社会：%s；%s；%s\n语言：%s\n"
        "统一影像风格：%s；%s；%s；画幅 %s"
        % (bp.get("title", ""), bp.get("genre", ""),
           ("[%s] " % w["setting_type"]) if w.get("setting_type") else "",
           w.get("era", ""), w.get("place", ""), w.get("tone", ""),
           w.get("geography", "") or "（未注明）", w.get("history", "") or "（未注明）",
           w.get("people", "") or "（未注明）",
           w.get("culture", ""), w.get("social_order", ""), w.get("tech_level", ""),
           bp.get("language", "zh-CN"),
           v.get("palette", ""), v.get("film_stock", ""), v.get("lighting", ""),
           v.get("aspect", "9:16"))
    )


def _cast_brief(bp, ids=None):
    out = []
    for c in bp.get("characters", []):
        if ids and c.get("id") not in ids and c.get("name") not in ids:
            continue
        out.append("%s(%s) %s｜性别：%s｜年龄：%s｜族裔：%s｜阶层：%s｜外形：%s｜服饰：%s"
                   % (c.get("name", ""), c.get("id", ""), c.get("role", ""),
                      c.get("gender", "") or "未注明", c.get("age", ""),
                      c.get("ethnicity", "") or "同 world.people",
                      c.get("social_class", "") or "未注明",
                      c.get("appearance", ""), c.get("costume", "")))
    return "\n".join(out)


def _voice_brief(bp, ids=None):
    """Per-character voice casting, for the VO director and for CosyVoice2.

    Accepts both the old free-text `voice` and the structured profile, because
    blueprints written before the schema grew must keep working."""
    out = []
    for c in bp.get("characters", []):
        if ids and c.get("id") not in ids and c.get("name") not in ids:
            continue
        v = c.get("voice")
        if isinstance(v, dict):
            desc = "音色 %s｜音域 %s｜语速 %s｜口音 %s｜习惯 %s" % (
                v.get("timbre", ""), v.get("register", ""), v.get("pace", ""),
                v.get("accent", ""), v.get("quirks", ""))
        else:
            desc = str(v or "")
        out.append("%s(%s) %s岁 %s｜%s"
                   % (c.get("name", ""), c.get("id", ""), c.get("age", ""),
                      c.get("role", ""), desc))
    return "\n".join(out)


def _loc_brief(bp, ids=None):
    out = []
    for l in bp.get("locations", []):
        if ids and l.get("id") not in ids and l.get("name") not in ids:
            continue
        out.append("%s(%s) %s｜时段：%s｜%s"
                   % (l.get("name", ""), l.get("id", ""),
                      l.get("interior_exterior", ""),
                      "、".join(l.get("times_of_day") or ["日"]),
                      l.get("description", "")))
    return "\n".join(out)


def _clip(text, n=90):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# Four views per character, passport-style: the whole body in frame, centred,
# neutral, on a plain ground. Same seed for all four so the figure has the
# best chance of being the same person from every side; the head says which
# side, the model's own text describes the person and must not say 正面.
# Left and right are spelled out as screen direction, in Chinese and English,
# because "身体朝向画面右方" alone was not enough: with the shared seed the
# model reused the left-profile composition for the right view (ch04 came out
# facing left twice). Nose toward the frame edge is the cue that sticks.
CHAR_VIEWS = [
    ("",       "正面", "正面全身，正对镜头，双臂自然下垂（front view, facing the camera）"),
    ("_left",  "左侧", "标准左侧面全身（left side profile view）：人物面朝画面左侧，鼻尖指向画面左边缘，"
                       "观众只看到人物的左半边脸与左肩，右半身被完全遮住；身体另一侧的特征此角度不可见"),
    ("_right", "右侧", "标准右侧面全身（right side profile view, facing right）：人物面朝画面右侧，鼻尖指向"
                       "画面右边缘，观众只看到人物的右半边脸与右肩，左半身被完全遮住；身体另一侧的特征此角度不可见"),
    ("_back",  "背面", "背面全身（back view）：背对镜头，完全看不到脸，发型与衣服背面清楚可见；脸上的特征此角度不可见"),
]


# Nine views per scene, one seed per (location, time of day): the outside from
# four sides, the inside from four sides, and straight down. "内" is read as
# "inside the place" so an open-air location (a wharf) still gets its nine.
LOC_VIEWS = [
    # (suffix, label, what the camera is doing / what is and is not in frame)
    ("_ext_front", "外·正面",
     "建筑外观·正面（exterior, street view of the front gate）：站在院墙/建筑外的街巷上，正对大门平视。"
     "画面主体是大门、门楣、外墙面与墙头瓦、露出墙头的屋顶和树冠、门前的街面；院内/室内的桌椅陈设全部被墙挡住，一样都看不见"),
    ("_ext_back",  "外·背面",
     "建筑外观·背面（exterior, rear elevation, the back of the building）：站在建筑后方的巷子里平视后墙。画面主体是后墙外侧、屋脊背面与后檩、后窗或后门、"
     "墙根的杂草与排水；这是背面，画面里没有大门、没有匾额、没有石狮、没有门前石阶，院内陈设也看不见"),
    ("_ext_left",  "外·左侧",
     "建筑外观·左侧（exterior, left side elevation）：站在建筑左侧的巷子里平视左侧外墙。画面主体是左山墙、马头墙轮廓、侧面屋檩与瓦当、"
     "侧巷地面；这是侧面，画面里没有大门、没有匾额、没有石狮，也看不到院内陈设"),
    ("_ext_right", "外·右侧",
     "建筑外观·右侧（exterior, right side elevation）：站在建筑右侧的巷子里平视右侧外墙。画面主体是右山墙、马头墙轮廓、侧面屋檩与瓦当、"
     "侧巷地面；这是侧面，画面里没有大门、没有匾额、没有石狮，也看不到院内陈设"),
    ("_int_front", "内·正面",
     "内部·正面（interior, standing just inside the entrance looking straight ahead）：从大门内侧向正前方的主屋/主厅看去。"
     "画面主体是正对入口的那一面：主位的家具正面朝着镜头（案在前、主位椅在案后），左右两侧只在画面边缘露一点；入口在镜头身后，不在画面里"),
    ("_int_back",  "内·背面",
     "内部·背面（interior, from the far end looking back toward the entrance）：站在最里面、主位的身后，回身看大门方向。"
     "空间与「内·正面」是同一个房间掉头看：画面最远处是大门的内侧和门洞里透出的外面，近处是主位家具的背面——椅背朝着镜头，案在椅子前面更远一点；"
     "两侧的陈设左右互换位置"),
    # Saying the main desk "shows at the frame's edge" was enough for the model
    # to put it back in the centre. For the side walls it is simply not there.
    ("_int_left",  "内·左侧",
     "内部·左侧（interior, camera turned 90 degrees, a straight-on view of the LEFT wall filling the frame）：站在房间中央，"
     "镜头转向左侧那一面墙，与房间的纵轴垂直。这面墙和贴墙摆的木架/陈设正对镜头、占满整个画面宽度，构图像一张墙面立面图；"
     "主位的大案、主位椅、大门、挂画的正墙都在镜头的两侧之外，不在画面里"),
    ("_int_right", "内·右侧",
     "内部·右侧（interior, camera turned 90 degrees, a straight-on view of the RIGHT wall filling the frame）：站在房间中央，"
     "镜头转向右侧那一面墙，与房间的纵轴垂直。这面墙和贴墙摆的木架/陈设正对镜头、占满整个画面宽度，构图像一张墙面立面图；"
     "主位的大案、主位椅、大门、挂画的正墙都在镜头的两侧之外，不在画面里"),
    ("_top",       "俯瞰",
     "垂直正俯视（top-down plan view, drone camera pointing straight down）：没有地平线、没有天空，画面全是屋顶瓦面、院落/场地的地面布局、"
     "树冠顶部和四周的巷子，像一张带材质的平面图"),
]


def _anchor(bp, char=None, gender=None, chars=None, kind=None, view=None):
    """What goes around every image and video prompt: a short subject-first
    head, and the long world context as a tail.

    Two failures shaped this. ch01 rendered as a European woman because the
    model wrote costume in period words (褙子、木簪) a diffusion text encoder
    cannot decode -- so era, ethnicity and gender are stated here in plain
    words, in code, every time. Then ch02 rendered as a fishing scene because
    the first version of this put the whole world paragraph in FRONT: 运河、
    漕运、木帆船、油灯 arrived before the model ever heard 人物, and the encoder
    weights the opening tokens. A portrait prompt has to open with the
    portrait. So: head = what the picture IS plus who/when/where, in one line;
    tail = culture, society, geography, history, appended after the model's
    own text where it informs without competing.

    kind: character | shot | location | prop  (inferred from char / chars)
    Returns {"head": str, "tail": str}; both may be "".
    """
    w = bp.get("world", {}) or {}
    kind = kind or ("character" if char is not None else "shot" if chars else "location")
    when = "，".join(_clip(w.get(k), 40) for k in ("era", "place") if w.get(k))
    people = _clip(w.get("people"), 40)

    def person(c, g=None):
        g = (g or c.get("gender") or "").strip()
        g = {"男": "男性", "女": "女性"}.get(g, g)
        bits = [c.get("ethnicity") or people, g,
                ("%s岁" % c["age"]) if c.get("age") else "", c.get("social_class") or ""]
        return "，".join(_clip(x, 40) for x in bits if x)

    if kind == "character":
        who = person(char or {}, gender)
        view = view or CHAR_VIEWS[0][2]
        head = ("单人全身人物参考图，证件照式：%s，全身从头到脚完整入镜、居中、中性表情，"
                "纯色干净背景，无场景、无道具。人物：%s。" % (
                    view, "，".join(x for x in (when, who) if x) or "见下文"))
        # The things a diffusion model gets wrong on its own, said outright:
        # one sword became two, iron-soled boots became lace-ups, a fresh
        # wound appeared on an arm nobody described.
        fit = ("面孔、发型与服饰须符合上述时代、地域与族裔；随身物件的数量以描述为准，一把刀就是一把刀，插在鞘中，"
               "不多画；鞋履为该时代样式，没有鞋带、拉链等现代细节；描述里的疤痕都是愈合多年的浅色旧疤，"
               "身上没有新鲜伤口和血迹；肢体与物件不重复")
    elif kind == "shot":
        named = "；".join("%s（%s）" % (c.get("name", c.get("id", "")), person(c))
                          for c in (chars or []))
        head = "%s%s" % (("%s。" % when) if when else "", ("画中人物：%s。" % named) if named else "")
        fit = "人物面孔、服饰与场景器物须符合上述时代、地域与族裔"
    elif kind == "prop":
        head = "白背景单体产品图，一件物品，结构正确，每个部件只有一个，无人物、无场景%s。" % (("，%s" % when) if when else "")
        fit = "器物形制与材质须符合上述时代与地域"
    else:
        head = "空景无人%s%s。" % (("，%s" % view) if view else "", ("，%s" % when) if when else "")
        # 旗杆 in a Ming yamen description came out as steel flagpoles with red
        # flags. The word is banned from the text and the tail says so too.
        # No "没有旗帜" here either: a diffusion model hears the noun, not the
        # negation. The word is stripped from the text and the verifier checks.
        fit = "建筑、器物与陈设须符合上述时代、地域与文化，没有电线、路灯、玻璃窗等现代物"
        if view:
            fit = "机位再说一次：%s。%s" % (view.split("：", 1)[0], fit)

    # Tech level is a list of objects (木帆船、油灯、铜算盘). It belongs behind a
    # location or a shot, and has no business anywhere near a portrait or a
    # single prop, where it reads as things to draw.
    fields = [("设定", "setting_type"), ("地理", "geography"), ("历史局势", "history"),
              ("文化", "culture"), ("社会", "social_order")]
    if kind in ("location", "shot"):
        fields.append(("技术水平", "tech_level"))
    ctx = "；".join("%s：%s" % (lab, _clip(w.get(k))) for lab, k in fields if w.get(k))
    tail = ("背景设定：%s。%s，不得出现现代或西式元素。" % (ctx, fit)) if ctx else (
        "%s，不得出现现代或西式元素。" % fit if when else "")
    return {"head": head if when or kind != "shot" else "", "tail": tail}


def _anchored(body, a):
    """head + the model's own text + tail. `a` is what _anchor returned."""
    body = " ".join(str(body or "").split())
    if not body:
        return ""
    return " ".join(x for x in ((a or {}).get("head", ""), body, (a or {}).get("tail", "")) if x)


# ----------------------------------------------------------------------
# 1 - the blueprint
# ----------------------------------------------------------------------
BP_SCHEMA = """{
 "title":"","logline":"一句话故事","genre":"","language":"zh-CN",
 "world":{"setting_type":"真实历史/架空幻想/现代/未来","era":"具体年代或纪年","place":"国家与地区",
   "geography":"地形、气候与植被——它决定建筑材料、衣料厚薄与光线","history":"故事发生时的历史局势或世界大事，一两句",
   "people":"人物族裔与面孔基调（如：汉人、东亚面孔）","culture":"","tech_level":"","social_order":"","tone":""},
 "visual_style":{"palette":"","film_stock":"","lens":"","lighting":"","aspect":"9:16","reference":""},
 "audio_style":{"score":"配乐风格与配器","ambience_bed":"全片环境声底","mix_note":"对白/环境/音乐的相对比重","language_register":"文白程度与称谓习惯"},
 "characters":[{"id":"ch01","name":"","role":"主角/对手/配角","tier":"主要/配角/NPC","gender":"男/女","age":"","ethnicity":"族裔（与 world.people 相同可留空）",
   "social_class":"身份阶层（士/农/工/商/官/兵/仆…），它决定衣料、颜色与配饰","appearance":"不变的外形特征",
   "costume":"惯常服饰","signature_prop":"随身物件","traits":[],"arc":"",
   "voice":{"timbre":"沙哑/清亮/浑厚/尖细","register":"低沉/中/偏高","pace":"缓/中/急",
     "accent":"官话/吴语口音/军中腔","quirks":"口头禅、习惯性停顿、气口"}}],
 "locations":[{"id":"lo01","name":"","interior_exterior":"内景/外景","description":"",
   "materials":"建筑与陈设材质","times_of_day":["日","夜"],"props":[]}],
 "props":[{"id":"pr01","name":"","description":"","owner":"ch01"}],
 "chapters":[{"n":1,"title":"","summary":"","beats":[],"characters":["ch01"],"locations":["lo01"]}]
}"""

BP_SYS = ("你是影视化开发的策划总监。你的产出是一份“制作圣经”，后面所有分镜、"
          "参考图与视频都必须照它执行，因此每个字段都要具体、可执行、可复现，"
          "不要写空泛的形容词。只输出 JSON，不要解释。")


class BlueprintReq(BaseModel):
    slug: Optional[str] = None
    premise: str = ""
    genre: str = "武侠"
    language: str = "zh-CN"
    chapters: int = 6
    # 0 = derive it: a novel that carries N chapters needs roughly 4N faces to
    # populate them. Set it explicitly to override.
    cast: int = 0
    style: str = ""
    notes: str = ""


def _norm_title(t):
    return re.sub(r"[\s·・…。，,.、—\-《》「」『』]", "", (t or "")).lower()


def _fix_duplicate_titles(bp):
    """Chapter titles must differ from each other.

    The model reuses one often enough to matter: a finished blueprint opened on
    《三文钱》 and closed on 《三文钱》, which reads as the same chapter twice.
    Regenerating the whole bible over a title would cost twelve minutes, so only
    the colliding chapters go back, with the titles already spoken for. Returns
    (n, old, new) for each rename, and the chapters it could not fix.
    """
    chapters = bp.get("chapters") or []
    # Tracked by position: chapters compare equal when their contents happen to
    # match, and `c in clashes` would then rename the wrong one.
    seen, clash_ix = set(), []
    work = _norm_title(bp.get("title"))
    for i, c in enumerate(chapters):
        k = _norm_title(c.get("title"))
        if not k or k in seen or k == work:
            clash_ix.append(i)
        else:
            seen.add(k)
    if not clash_ix:
        return [], []
    clash_set = set(clash_ix)
    clashes = [chapters[i] for i in clash_ix]
    taken = [c.get("title", "") for i, c in enumerate(chapters) if i not in clash_set]
    user = (
        "下面这些章的标题与其他章重复、与作品名相同、或者为空，请各起一个新标题。\n"
        "要求：\n"
        "- 不得与这些已用标题重复：%s\n"
        "- 不得与作品名《%s》相同。\n"
        "- 彼此之间也不得重复。\n"
        "- 贴合该章内容，2-8 字，句式不要雷同。\n\n%s\n\n"
        '输出格式：{"chapters":[{"n":1,"title":""}]}'
        % ("、".join("《%s》" % t for t in taken if t) or "（无）",
           bp.get("title", ""),
           "\n".join("第%d章（原题《%s》）：%s"
                     % (c.get("n"), c.get("title", ""), (c.get("summary") or "")[:140])
                     for c in clashes)))
    try:
        got = _chat_json("你是编剧统筹。只输出 JSON，不要解释。", user,
                         effort="medium", max_tokens=2048)
    except Exception:                                  # noqa: BLE001
        return [], clashes
    ix_by_n = {}
    for i in clash_ix:
        ix_by_n.setdefault(chapters[i].get("n"), i)
    used = {_norm_title(t) for t in taken} | {work}
    renamed, fixed_ix = [], set()
    for row in (got.get("chapters") or []):
        i = ix_by_n.get(row.get("n"))
        new = (row.get("title") or "").strip()
        if i is None or i in fixed_ix or not new or _norm_title(new) in used:
            continue
        c = chapters[i]
        renamed.append((c.get("n"), c.get("title", ""), new))
        c["title"] = new
        used.add(_norm_title(new))
        fixed_ix.add(i)
    return renamed, [chapters[i] for i in clash_ix if i not in fixed_ix]


# ----------------------------------------------------------------------
# versions
# ----------------------------------------------------------------------
# Regenerating used to overwrite. One chapter was rewritten four times in fifty
# minutes and the first three drafts are simply gone -- and the fourth was not
# obviously the best one. So every generation is kept, and one of them is the
# active version.
#
# `chapters/04.json` and `script/04.json` stay exactly where they were and keep
# holding the active version, so web_server, the Library page and every later
# step read them without knowing any of this exists. History lives beside them
# under `_versions/`.
#
# A script is a reading of a particular draft of the prose, so each script
# version records which chapter version it was cut from. When the prose is
# regenerated, the old shot list is not wrong -- it just belongs to a different
# draft, and the console can say so instead of silently pairing them.

_VER_KINDS = {"chapter": "chapters", "script": "script"}


def _ver_dir(slug, kind, n):
    return _proj_dir(slug, "_versions", _VER_KINDS[kind], "%02d" % int(n))


def _ver_live(slug, kind, n):
    """The path the rest of the pipeline reads."""
    return _proj_dir(slug, "%s/%02d.json" % (_VER_KINDS[kind], int(n)))


def _ver_list(slug, kind, n):
    d = _ver_dir(slug, kind, n)
    if not os.path.isdir(d):
        return []
    out = []
    for f in os.listdir(d):
        if f.endswith(".json") and f[:-5].isdigit():
            out.append(int(f[:-5]))
    return sorted(out)


def _ver_read(slug, kind, n, v):
    p = os.path.join(_ver_dir(slug, kind, n), "%d.json" % int(v))
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _ver_active(slug, kind, n):
    p = os.path.join(_ver_dir(slug, kind, n), "_active.json")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                return int(json.load(f).get("v"))
        except Exception:                              # noqa: BLE001
            pass
    vs = _ver_list(slug, kind, n)
    return vs[-1] if vs else None


def _ver_write_active(slug, kind, n, v):
    d = _ver_dir(slug, kind, n)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "_active.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"v": int(v)}, f)
    os.replace(tmp, os.path.join(d, "_active.json"))


def _ver_adopt(slug, kind, n):
    """A project written before versioning still has only the live file. Take
    it in as version 1 rather than pretending it never happened."""
    if _ver_list(slug, kind, n):
        return
    live = _ver_live(slug, kind, n)
    if not os.path.isfile(live):
        return
    with open(live, encoding="utf-8") as f:
        obj = json.load(f)
    obj.setdefault("_meta", {"v": 1, "created": int(os.path.getmtime(live)),
                             "note": "版本化之前就存在的内容"})
    d = _ver_dir(slug, kind, n)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "1.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    _ver_write_active(slug, kind, n, 1)


def _save_versioned(slug, kind, n, obj, extra=None):
    """Write a new version and make it the active one."""
    _ver_adopt(slug, kind, n)
    v = (_ver_list(slug, kind, n) or [0])[-1] + 1
    meta = {"v": v, "created": int(time.time())}
    meta.update(extra or {})
    obj["_meta"] = meta
    d = _ver_dir(slug, kind, n)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "%d.json.tmp" % v)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, os.path.join(d, "%d.json" % v))
    _ver_write_active(slug, kind, n, v)
    _save(slug, "%s/%02d.json" % (_VER_KINDS[kind], int(n)), obj)
    return v


def _ver_summary(slug, kind, n):
    """What the console needs to draw one tab per version."""
    _ver_adopt(slug, kind, n)
    active = _ver_active(slug, kind, n)
    rows = []
    for v in _ver_list(slug, kind, n):
        obj = _ver_read(slug, kind, n, v) or {}
        meta = obj.get("_meta") or {}
        row = {"v": v, "active": v == active, "created": meta.get("created")}
        if kind == "chapter":
            row["words"] = len(obj.get("prose") or "")
            row["title"] = obj.get("title") or ""
        else:
            shots = obj.get("shots") or []
            row["shots"] = len(shots)
            row["lines"] = sum(len(s.get("dialog") or []) for s in shots)
            row["narrated"] = len([s for s in shots
                                   if (s.get("narration") or "").strip()])
            row["voiced"] = len([s for s in shots
                                 if (s.get("dialog") or [])
                                 or (s.get("narration") or "").strip()])
            row["from_chapter_v"] = meta.get("from_chapter_v")
        rows.append(row)
    return rows


class VersionReq(BaseModel):
    slug: str
    n: int
    kind: str            # "chapter" | "script"
    v: int


def _need_kind(kind):
    if kind not in _VER_KINDS:
        raise HTTPException(status_code=400, detail="kind 必须是 chapter 或 script")


@app.get("/versions/{slug}/{n}")
def list_versions(slug: str, n: int):
    chapters = _ver_summary(slug, "chapter", n)
    scripts = _ver_summary(slug, "script", n)
    live_ch = _ver_active(slug, "chapter", n)
    for s in scripts:
        # A shot list cut from a draft that is no longer the active prose is
        # not wrong, it is just answering an older question. Say which.
        s["stale"] = bool(s.get("from_chapter_v")
                          and live_ch and s["from_chapter_v"] != live_ch)
    return {"slug": slug, "n": n, "chapters": chapters, "scripts": scripts}


@app.post("/versions/activate")
def activate_version(req: VersionReq):
    _need_kind(req.kind)
    obj = _ver_read(req.slug, req.kind, req.n, req.v)
    if obj is None:
        raise HTTPException(status_code=404,
                            detail="第 %d 章没有 %s 的第 %d 版" % (req.n, req.kind, req.v))
    _ver_write_active(req.slug, req.kind, req.n, req.v)
    _save(req.slug, "%s/%02d.json" % (_VER_KINDS[req.kind], int(req.n)), obj)
    print("[story] 第%d章 %s 切换到第 %d 版" % (req.n, req.kind, req.v), flush=True)
    return {"ok": True, "active": req.v}


@app.post("/versions/delete")
def delete_version(req: VersionReq):
    _need_kind(req.kind)
    vs = _ver_list(req.slug, req.kind, req.n)
    if req.v not in vs:
        raise HTTPException(status_code=404, detail="没有第 %d 版" % req.v)
    if len(vs) <= 1:
        raise HTTPException(status_code=400,
                            detail="这是仅剩的一版，删掉就什么都不剩了")
    os.remove(os.path.join(_ver_dir(req.slug, req.kind, req.n), "%d.json" % req.v))
    left = _ver_list(req.slug, req.kind, req.n)
    active = _ver_active(req.slug, req.kind, req.n)
    if active == req.v or active not in left:
        newest = left[-1]
        _ver_write_active(req.slug, req.kind, req.n, newest)
        _save(req.slug, "%s/%02d.json" % (_VER_KINDS[req.kind], int(req.n)),
              _ver_read(req.slug, req.kind, req.n, newest))
        print("[story] 第%d章 %s 删除第 %d 版（原为当前版），改用第 %d 版"
              % (req.n, req.kind, req.v, newest), flush=True)
    else:
        print("[story] 第%d章 %s 删除第 %d 版" % (req.n, req.kind, req.v), flush=True)
    return {"ok": True, "left": left, "active": _ver_active(req.slug, req.kind, req.n)}


@app.post("/blueprint")
def make_blueprint(req: BlueprintReq):
    if not (req.premise or "").strip():
        raise HTTPException(status_code=400, detail="premise 不能为空")
    user = (
        "根据下面的构思，写一份可执行的制作圣经。\n"
        "要求：\n"
        "- 人物恰好 %d 位，用 tier 分三档：主要 2-4 位（贯穿全片）、配角 4-8 位"
        "（多章出现）、其余为 NPC（一两章的功能性角色，如船工、衙役、掌柜、更夫）。\n"
        "- 每位的 appearance 与 costume 必须是“同一个人每次出场都长这样”的固定特征"
        "（发型、五官、身形、疤痕、惯常衣着与颜色），不要写情绪或动作。\n"
        "- NPC 也要有一处独有的可辨识特征（豁牙、瘸腿、左脸烫疤、独眼），"
        "宁可简短也不要写成谁都适用的通用描述——通用描述等于没写。\n"
        "- 每一章的 characters 要把该章出场的 NPC 也列进去，全流程靠 id 对齐。\n"
        "- 场景 3-6 处，写清材质、光线来源与陈设，供后面出参考图。\n"
        "- visual_style 全片统一，后面每个提示词都会引用它。\n"
        "- world 的每一栏都要填实：setting_type（真实历史还是架空幻想）、era、place、"
        "geography、history、people、culture、tech_level、social_order。每位人物 gender、"
        "social_class 必填。这些会原文写进后面每一条图像与视频提示词的开头——文生图模型"
        "看不懂“褙子”“襦裙”背后的朝代、地域与性别，只认大白话。\n"
        "- chapters 恰好 %d 章，每章给 3-5 个 beats（事件节拍）。\n"
        "- 每章标题必须互不相同，也不要与作品名相同；开篇与结尾章尤其不要用同一个"
        "词收尾呼应，那会让人以为是同一章。标题的字数与句式要有变化，不要六章全是"
        "两字名词（如“落笔／墨痕／堂上”），可以混用动词短语、物件、一句话。\n"
        "- 所有 id 用 ch01/lo01/pr01 这样的编号，后面全流程靠 id 对齐。\n"
        "%s%s\n输出格式：\n%s\n\n构思：\n%s"
        # order matters: 人物 %d comes before chapters %d in the text above
        % (max(3, int(req.cast) if req.cast else int(req.chapters) * 4),
           max(1, int(req.chapters)),
           ("统一影像风格倾向：%s\n" % req.style) if req.style else "",
           ("额外要求：%s\n" % req.notes) if req.notes else "",
           BP_SCHEMA, req.premise.strip())
    )
    t0 = time.time()
    _prog_begin("蓝图", "生成制作圣经")
    try:
        # The whole production bible in one reply -- the largest output in
        # the pipeline, and thinking tokens share the budget. 8192 truncated
        # it mid-object more than once.
        bp = _chat_json(BP_SYS, user, effort="high", max_tokens=32768,
                        require=lambda o: o.get("characters") and o.get("chapters"))
    except Exception:
        _prog_end("蓝图失败")
        raise
    bp.setdefault("language", req.language)
    bp.setdefault("genre", req.genre)
    slug = _slug(req.slug or bp.get("title") or req.premise[:30])
    bp["slug"] = slug
    bp["premise"] = req.premise.strip()
    bp["revision"] = 1
    renamed, unfixed = _fix_duplicate_titles(bp)
    for n, old, new in renamed:
        print("[story]   第%d章标题重复《%s》-> 改为《%s》" % (n, old, new), flush=True)
    for c in unfixed:
        print("[story] ! 第%d章标题《%s》仍与其他章重复，改名失败，请人工改"
              % (c.get("n"), c.get("title", "")), flush=True)
    _save(slug, "blueprint.json", bp)
    _prog_end("蓝图完成 · %s" % (bp.get("title") or slug))
    print("[story] blueprint '%s' in %ds: %d chars, %d locs, %d chapters"
          % (slug, int(time.time() - t0), len(bp.get("characters", [])),
             len(bp.get("locations", [])), len(bp.get("chapters", []))), flush=True)
    return bp


class ReviseReq(BaseModel):
    slug: str
    notes: str


# The revision used to ask the model to re-emit the whole blueprint. That fails
# for the same reason every time: a finished bible is 10-15KB of JSON, and a 27B
# at q4 drops a comma somewhere past ten thousand characters. Retrying an
# impossible request three times just fails three times.
#
# So the model now returns a PATCH -- only what changes -- typically a few
# hundred bytes, and the merge happens here in Python where it cannot go wrong.
PATCH_SCHEMA = """{
 "set": {"顶层或嵌套字段，只写要改的": "例如 title、logline、world.era、visual_style.palette"},
 "characters": [{"id":"ch01","只写要改的字段":"其余不动"}],
 "locations":  [{"id":"lo01","...":"..."}],
 "props":      [{"id":"pr01","...":"..."}],
 "chapters":   [{"n":3,"...":"..."}],
 "add":    {"characters":[],"locations":[],"props":[]},
 "remove": {"characters":[],"locations":[],"props":[]}
}"""


def _deep_merge(base, patch):
    """Merge patch into base. Dicts merge recursively; everything else
    (scalars, lists) replaces, because a list the author rewrote is a
    replacement, not an append."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return patch
    out = dict(base)
    for k, v in patch.items():
        out[k] = _deep_merge(out.get(k), v) if isinstance(v, dict) else v
    return out


def _merge_by_key(items, patches, key):
    """Apply per-item patches matched on `key` (id, or n for chapters).
    Unmatched patches are ignored rather than appended -- adding is what the
    explicit `add` block is for, and a typo'd id must not silently create a
    seventh character."""
    out = [dict(i) for i in (items or [])]
    index = {i.get(key): i for i in out}
    touched, ignored = [], []
    for p in (patches or []):
        if not isinstance(p, dict):
            continue
        ident = p.get(key)
        target = index.get(ident)
        if target is None:
            ignored.append(ident)
            continue
        target.update(_deep_merge(target, p))
        touched.append(ident)
    return out, touched, ignored


def _apply_patch(bp, patch):
    """Returns (new_blueprint, human-readable change list)."""
    new = _deep_merge(bp, patch.get("set") or {})
    changes = []
    for field, value in (patch.get("set") or {}).items():
        changes.append("set %s" % field)

    for field, key in (("characters", "id"), ("locations", "id"),
                       ("props", "id"), ("chapters", "n")):
        if not patch.get(field):
            continue
        merged, touched, ignored = _merge_by_key(new.get(field), patch[field], key)
        new[field] = merged
        if touched:
            changes.append("%s 改 %s" % (field, ",".join(str(t) for t in touched)))
        if ignored:
            changes.append("%s 忽略未知 %s" % (field, ",".join(str(i) for i in ignored)))

    add = patch.get("add") or {}
    for field in ("characters", "locations", "props"):
        rows = [r for r in (add.get(field) or []) if isinstance(r, dict) and r.get("id")]
        if rows:
            have = {i.get("id") for i in new.get(field, [])}
            fresh = [r for r in rows if r["id"] not in have]
            new[field] = (new.get(field) or []) + fresh
            if fresh:
                changes.append("%s 新增 %s" % (field, ",".join(r["id"] for r in fresh)))

    rem = patch.get("remove") or {}
    for field in ("characters", "locations", "props"):
        ids = [i for i in (rem.get(field) or []) if i]
        if ids:
            before = len(new.get(field) or [])
            new[field] = [i for i in (new.get(field) or []) if i.get("id") not in ids]
            if len(new[field]) != before:
                changes.append("%s 删除 %s" % (field, ",".join(ids)))
    return new, changes


@app.post("/blueprint/revise")
def revise_blueprint(req: ReviseReq):
    """The human-in-the-loop step. Cheap here, ruinous later."""
    bp = _need_blueprint(req.slug)
    if not (req.notes or "").strip():
        raise HTTPException(status_code=400, detail="notes 不能为空")

    sys_p = ("你是制作圣经的维护者。作者提出修改意见，你只输出一个「补丁」，"
             "描述哪些字段要变成什么，不要重复输出没有变化的内容。"
             "补丁越小越好。id 是全流程的对齐依据，绝对不要改动已有 id。"
             "只输出 JSON，不要解释。")
    user = (
        "作者的修改意见：\n%s\n\n"
        "规则：\n"
        "- 只写需要改动的部分；没提到的一律不要出现在补丁里。\n"
        "- 改已有条目用 characters/locations/props/chapters，靠 id（章节靠 n）定位。\n"
        "- 新增条目放进 add，并自己起一个没用过的 id。\n"
        "- 删除条目把 id 放进 remove。\n"
        "- 顶层或嵌套字段（title、logline、world.*、visual_style.*、audio_style.*）放进 set。\n"
        "\n补丁格式：\n%s\n\n现有制作圣经：\n%s"
        % (req.notes.strip(), PATCH_SCHEMA, json.dumps(bp, ensure_ascii=False))
    )

    _prog_begin("修改蓝图", "生成补丁")
    try:
        patch = _chat_json(sys_p, user, effort="high", max_tokens=3072)
    except Exception:
        _prog_end("修改蓝图失败")
        raise

    if not isinstance(patch, dict) or not any(
            patch.get(k) for k in ("set", "characters", "locations", "props",
                                   "chapters", "add", "remove")):
        _prog_end("修改蓝图：补丁为空")
        raise HTTPException(status_code=422,
                            detail="模型没有给出任何改动。把意见写得更具体一些再试。")

    new, changes = _apply_patch(bp, patch)
    # Identity fields are ours, not the model's.
    new["slug"] = bp["slug"]
    new["premise"] = bp.get("premise", "")
    new["revision"] = int(bp.get("revision", 1)) + 1

    _save(req.slug, "revisions/blueprint-r%d.json" % bp.get("revision", 1), bp)
    _save(req.slug, "revisions/patch-r%d.json" % new["revision"],
          {"notes": req.notes, "patch": patch, "changes": changes})
    _save(req.slug, "blueprint.json", new)
    _prog_end("蓝图 -> 第 %d 版" % new["revision"])
    print("[story] blueprint '%s' -> revision %d; %s"
          % (req.slug, new["revision"], "；".join(changes) or "(无实际改动)"), flush=True)
    new["_changes"] = changes
    return new


# ----------------------------------------------------------------------
# 2 - chapters, written one at a time
# ----------------------------------------------------------------------
class ChapterReq(BaseModel):
    slug: str
    n: int = 1
    words: int = 1200
    notes: str = ""


@app.post("/chapter")
def write_chapter(req: ChapterReq):
    bp = _need_blueprint(req.slug)
    outline = {c.get("n"): c for c in bp.get("chapters", [])}
    plan = outline.get(req.n)
    if not plan:
        raise HTTPException(status_code=400,
                            detail="蓝图里没有第 %d 章（共 %d 章）" % (req.n, len(outline)))

    # Continuity: every earlier chapter's summary, never the full prose -- the
    # summaries are what keep chapter 6 agreeing with chapter 1 without
    # spending the whole context window on it.
    prior = []
    for i in range(1, req.n):
        ch = _load(req.slug, "chapters/%02d.json" % i)
        if ch:
            prior.append("第%d章《%s》：%s" % (i, ch.get("title", ""), ch.get("summary", "")))

    sys_p = ("你是这部作品的执笔作者。严格遵守制作圣经里的世界观、人物设定与语言风格；"
             "人物的外形与服饰是固定的，不要改动。只输出 JSON，不要解释。")
    user = (
        "%s\n\n人物：\n%s\n\n场景：\n%s\n\n"
        "%s"
        "现在写第 %d 章《%s》，约 %d 字。\n本章要点：%s\n本章梗概：%s\n"
        "标题沿用上面这个即可；确要改动时，不得与其他各章重复（已用：%s）。\n%s"
        '\n输出格式：{"n":%d,"title":"","prose":"正文","summary":"200字以内的梗概，供后面各章保持连贯",'
        '"characters":["ch01"],"locations":["lo01"]}'
        % (_world_brief(bp), _cast_brief(bp), _loc_brief(bp),
           ("前情：\n%s\n\n" % "\n".join(prior)) if prior else "",
           req.n, plan.get("title", ""), max(200, int(req.words)),
           "；".join(plan.get("beats", []) or []), plan.get("summary", ""),
           "、".join("《%s》" % (c.get("title") or "")
                    for c in bp.get("chapters", []) if c.get("n") != req.n) or "无",
           ("额外要求：%s\n" % req.notes) if req.notes else "",
           req.n)
    )
    t0 = time.time()
    _prog_begin("正文", "第 %d 章《%s》" % (req.n, plan.get("title", "")),
                step=req.n, steps=len(outline))
    try:
        # Thinking shares this budget and takes most of it: a 1200-word
        # chapter truncated mid-prose at 4800. Give the prose real room.
        ch = _chat_json(sys_p, user, effort="high",
                        max_tokens=max(12288, int(req.words) * 8),
                        require=lambda o: len((o.get("prose") or "")) >= 200)
    except Exception:
        _prog_end("第 %d 章正文失败" % req.n)
        raise
    _prog_end("第 %d 章正文完成" % req.n)
    ch["n"] = req.n
    ch.setdefault("title", plan.get("title", ""))
    # The model is asked for a title and sometimes returns one another chapter
    # already carries -- worse than not asking at all. The blueprint is the
    # authority, so a collision falls back to it instead of shipping a
    # duplicate into every downstream prompt and file name.
    others = {_norm_title(c.get("title")) for c in bp.get("chapters", [])
              if c.get("n") != req.n}
    if not _norm_title(ch.get("title")) or _norm_title(ch.get("title")) in others:
        if (ch.get("title") or "") != plan.get("title", ""):
            print("[story]   第%d章标题《%s》与其他章重复，回退到蓝图的《%s》"
                  % (req.n, ch.get("title", ""), plan.get("title", "")), flush=True)
        ch["title"] = plan.get("title", "")
    v = _save_versioned(req.slug, "chapter", req.n, ch)
    print("[story] chapter %d of '%s' in %ds: %d chars（第 %d 版）"
          % (req.n, req.slug, int(time.time() - t0),
             len(ch.get("prose", "")), v), flush=True)
    return ch


# ----------------------------------------------------------------------
# 3 - the director's script
# ----------------------------------------------------------------------
# The controlled vocabulary. Without it a 27B writes "镜头拉近，气氛紧张" --
# a mood, not an instruction. With it the model picks from terms that mean
# something specific to a camera operator, a gaffer, an editor and a VO
# director, which is also what makes the output reusable as a real shot list.
CRAFT_LANGUAGE = """【摄影术语表 · 只能从这些里选】
景别：ELS 大远景 / LS 全景 / MLS 中全景 / MS 中景 / MCU 中近景 / CU 特写 / ECU 大特写
角度：平视 / 俯角 / 仰角 / 斜角(Dutch) / 过肩(OTS) / 主观(POV) / 鸟瞰 / 低角贴地
机位高度：地面 / 膝高 / 腰高 / 齐眼 / 过顶
运镜：固定(lock-off) / 摇(pan) / 俯仰(tilt) / 推(dolly in) / 拉(dolly out) /
      横移(truck) / 升降(pedestal) / 摇臂(crane) / 环绕(arc) / 手持 / 斯坦尼康 /
      变焦(zoom) / 甩镜(whip pan) / 推轨变焦(dolly zoom)
镜头：14/24/35/50/85/135mm；光圈 T1.4-T5.6；景深 浅/中/深
构图：三分法 / 中心构图 / 对称 / 框中框 / 前景遮挡 / 引导线；注意头顶留白与视线留白
对焦：固定焦点 / 变焦点(rack focus，从X到Y) / 深焦
轴线：给出人物朝向与运动方向，同一场戏不要越过 180 度轴线
速度：24fps 常速 / 升格慢放 / 降格快放 / 速度爬升(speed ramp)

【灯光术语表】
主光(key)方向与质感（硬光/柔光）、辅光(fill)比例、轮廓光(back/rim)、
画内光源(practical，如灯笼/油灯/窗)、光比（如 2:1 / 4:1 / 8:1）、
色温（如 2700K 暖 / 5600K 日光）、动机（光从哪来，必须讲得通）

【剪辑术语表】
切(cut) / 匹配剪辑(match cut) / 动作接动作(cut on action) / 叠化(dissolve) /
淡入淡出(fade) / 划(wipe) / 硬切(smash cut) / J-cut（先声后画）/ L-cut（声音延续）

【声音与配音术语表】
语速：缓 / 中 / 急    音量：耳语 / 常态 / 提高 / 喊
音高：低沉 / 中 / 偏高    音色：沙哑 / 清亮 / 浑厚 / 尖细
重音：指明要重读的那个词    停顿：指明在哪一句之后停
气口：吸气 / 叹息 / 屏息 / 颤音
潜台词(subtext)：这句话真正的意思，与字面往往不同
声音透视：近讲(on-mic) / 远场 / 画外(O.S.) / 旁白(V.O.)
声音层：环境底噪(room tone) / 环境声(ambience) / 拟音(foley) / 音效(SFX) /
        音乐（进点、出点、情绪、配器）；标明是画内音(diegetic)还是画外音"""

SHOT_SCHEMA = """{"n":1,"shots":[{
 "id":"C01S01","seconds":10,
 "slug_line":"内景 沈家小院 — 夜",
 "location_id":"lo01","time_of_day":"","weather":"",
 "characters":["ch01"],
 "beat":"这一镜在故事里承担什么（推进/揭示/铺垫/转折）",
 "action":"可拍摄的动作，一句话，不要心理描写",
 "blocking":"人物在画面里的走位与相对位置，以及镜头内的移动",
 "camera":{
   "shot_size":"MCU","angle":"平视","height":"齐眼",
   "lens_mm":50,"aperture":"T1.5","dof":"浅",
   "movement":"推(dolly in)","movement_motivation":"为什么这样动",
   "movement_speed":"慢","composition":"三分法，视线留白在右",
   "focus":"固定焦点在人物眼睛 / 或 变焦点：从手中信纸到人物眼睛",
   "screen_direction":"人物面向画右，运动方向画右",
   "frame_rate":"24fps 常速"},
 "lighting":{"key":"窗外月光，柔光，画左45度","fill":"无，暗部靠反射",
   "back":"油灯轮廓光","practical":"桌上油灯","ratio":"8:1",
   "color_temp":"2700K 暖","motivation":"光必须解释得通"},
 "transitions":{"in":"切","out":"L-cut，脚步声延续到下一镜"},
 "dialog":[{"who":"ch01","line":"台词原文","subtext":"这句话真正的意思",
   "delivery":{"emotion":"","pace":"缓","volume":"常态","pitch":"低沉",
     "timbre":"沙哑","emphasis":"要重读的词","pause":"停顿位置","breath":"叹息"}}],
 "performance":"表情与肢体语言，可拍摄的（眉、眼、嘴、手、肩、呼吸）",
 "sound":{"room_tone":"","ambience":"","foley":["脚步","衣料摩擦"],"sfx":[],
   "music":{"cue":"进/出/无","mood":"","instrument":""},
   "perspective":"近讲","diegetic":true},
 "narration":"旁白文本，没有就留空字符串（会送给 TTS 念）",
 "video_prompt":"送给视频模型的中文提示词，一句话：主体+动作+运镜+光线+统一风格",
 "first_frame_ref":"lo01 或 ch01，指定首帧参考图用哪张"
}]}"""


class ScriptReq(BaseModel):
    slug: str
    n: int = 1
    seconds: int = 10
    max_shots: int = 0          # 0 = let the chapter decide


@app.post("/script")
def make_script(req: ScriptReq):
    bp = _need_blueprint(req.slug)
    ch = _load(req.slug, "chapters/%02d.json" % req.n)
    if not ch:
        raise HTTPException(status_code=404,
                            detail="第 %d 章还没写，先 POST /chapter" % req.n)
    cap = ("最多 %d 个镜头。" % req.max_shots) if req.max_shots else \
          "镜头数量由内容决定，不要为了凑数拆散一个动作。"
    sys_p = ("你同时是摄影指导(DP)、剪辑指导和配音导演。你的产出是一份可以直接"
             "交给摄影组、灯光组和录音棚执行的镜头表，也会被送进视频模型和 TTS。"
             "因此每一栏都必须是“可执行的指令”，不是气氛描写：\n"
             "- 只用给定术语表里的词，不要自创。\n"
             "- 每个镜头都要给出机位、焦段、光比、光的动机、轴线方向。\n"
             "- 每句台词都要给潜台词与念法（语速/音量/音高/重音/停顿/气口）。\n"
             "- 你是在把一部小说改成有声影像，不是做默片：正文里的对白要保住，"
             "拍不出来的信息用旁白补上。\n"
             "- 禁止写心理活动；心理只能通过表情、动作、光线、声音体现。\n"
             "只输出 JSON，不要解释。")
    audio = bp.get("audio_style", {}) or {}
    user = (
        "%s\n\n人物：\n%s\n\n配音设定：\n%s\n\n场景：\n%s\n\n"
        "全片声音风格：配乐 %s；环境声底 %s；混音 %s；语言文白程度 %s\n\n"
        "%s\n\n"
        "把下面这一章拆成分镜。每镜严格 %d 秒。%s\n"
        "硬性要求：\n"
        "- location_id 与 characters 必须用制作圣经里的 id。\n"
        "- camera 的每一栏都要填，movement_motivation 必须解释为什么这样动。\n"
        "- 景别与运镜要有起伏。不要连着三镜用同一景别：每场戏至少要有一个"
        "大远景或远景把空间交代清楚，也至少要有一个特写落在决定性的细节上"
        "（手、字、眼睛、物证）。全章固定机位不要超过三分之二，其余分给"
        "推/拉/横移/摇/手持，每一次运动仍然要有说得通的 movement_motivation；"
        "机位高度也一样，不要整章都是齐眼。这不是为变化而变化：上一章二十镜"
        "里十七镜是齐眼中景固定机位，剪到一起每一刀都像没剪，观众看不出"
        "镜头换过。\n"
        "- lighting.motivation 必须说明光源在画内是什么，讲不通就换一个。\n"
        "- screen_direction 要保证同一场戏不越轴。\n"
        "- transitions.in/out 要和相邻镜头对得上（用 J-cut / L-cut 时说明是什么声音跨过去）。\n"
        "- 台词从正文里搬，不要另编，也不要省略：正文中人物说出口的每一句话都要"
        "落到某个镜头的 dialog 里。太长的可以拆到相邻两镜，但不能丢。拟声词"
        "（嘎吱、吱呀）不是台词，归到 sound.foley。\n"
        "- 每句台词都要有 subtext 与 delivery 全部子项。只有这一镜确实没人说话时"
        "才给空数组，不要为了省事给空数组。\n"
        "- narration 是本片主要的叙事手段之一，该用就用：正文交代了、但镜头拍不"
        "出来的东西——人物的推理与判断、律条与账册的内容、时间跳跃、书信与状纸上"
        "的文字——都写成旁白念出来，并把该镜 sound.perspective 设为“旁白(V.O.)”。"
        "narration 必须是可以直接朗读的成句中文，不是内容提要。\n"
        "- 全章至少三分之二的镜头要有台词或旁白。一整章几乎没有人声，等于把小说"
        "拍成了默片，那不是这个片子要的东西。\n"
        "- video_prompt 要能独立成立：不看上下文也说清楚谁、在哪、做什么、"
        "镜头怎么动、什么光线，并带上统一影像风格；一句话，不要堆术语。“谁”要用"
        "视频模型看得懂的大白话：朝代/地域 + 族裔 + 性别 + 年龄（“明代苏州的汉人"
        "青年男子”），服饰带朝代名。\n"
        "- first_frame_ref 指向本镜首帧参考图（场景 id 或人物 id）。\n"
        "\n输出格式：\n%s\n\n第 %d 章正文：\n%s"
        % (_world_brief(bp), _cast_brief(bp, ch.get("characters")),
           _voice_brief(bp, ch.get("characters")),
           _loc_brief(bp, ch.get("locations")),
           audio.get("score", ""), audio.get("ambience_bed", ""),
           audio.get("mix_note", ""), audio.get("language_register", ""),
           CRAFT_LANGUAGE,
           max(1, int(req.seconds)), cap, SHOT_SCHEMA, req.n, ch.get("prose", ""))
    )
    t0 = time.time()
    _prog_begin("分镜", "第 %d 章《%s》" % (req.n, ch.get("title", "")),
                step=req.n, steps=len(bp.get("chapters", [])))
    try:
        # A full chapter of shots runs 12-15k tokens of JSON before thinking
        # is counted; 16384 truncated it and cost real shots (9 kept of a
        # longer list the model was still writing).
        sc = _chat_json(sys_p, user, effort="high", max_tokens=32768,
                        require=lambda o: o.get("shots"))
    except Exception:
        _prog_end("第 %d 章分镜失败" % req.n)
        raise
    _prog_end("第 %d 章分镜完成" % req.n)
    sc["n"] = req.n
    # Preserving a half-written value is right for prose and wrong for a list
    # of records: a truncated reply left C01S09 with a camera and no lighting,
    # which looks like a shot in the UI and cannot be lit, shot, or prompted.
    # Drop it rather than pass it downstream, and say so.
    raw_shots = sc.get("shots", []) or []
    # Partitioned in one pass: `s in shots` compared shots by value, so two
    # identical shots would have been reported wrongly.
    shots, dropped = [], []
    for s in raw_shots:
        (shots if (s.get("camera") and s.get("lighting")) else dropped).append(s)
    for s in dropped:
        print("[story] ! 丢弃残缺镜头 %s（缺 %s，多半是原文被截断）"
              % (s.get("id") or "?",
                 "、".join(k for k in ("camera", "lighting") if not s.get(k))),
              flush=True)
    by_id = {c.get("id"): c for c in bp.get("characters", [])}
    for i, s in enumerate(shots, 1):
        s.setdefault("id", "C%02dS%02d" % (req.n, i))
        s.setdefault("seconds", req.seconds)
        # Same rule as the reference images: the world goes in front of the
        # video prompt in code, with each character in the shot named by
        # gender and age, so slot C is not left to guess who these people are.
        raw = (s.get("video_prompt_raw") or s.get("video_prompt") or "").strip()
        if raw:
            s["video_prompt_raw"] = raw
            s["video_prompt"] = _anchored(
                raw, _anchor(bp, chars=[by_id[c] for c in (s.get("characters") or []) if c in by_id]))
    sc["shots"] = shots
    # Record which draft of the prose this reading was cut from.
    v = _save_versioned(req.slug, "script", req.n, sc,
                        extra={"from_chapter_v": _ver_active(req.slug, "chapter", req.n)})
    # Report the human voice, not just the shot count. Chapters were coming
    # back with four lines across nine shots and no narration at all -- a
    # silent film built from a novel full of talking -- and nothing in the
    # output made that visible until someone read the script.
    lines = sum(len(s.get("dialog") or []) for s in shots)
    narrated = len([s for s in shots if (s.get("narration") or "").strip()])
    voiced = len([s for s in shots
                  if (s.get("dialog") or []) or (s.get("narration") or "").strip()])
    print("[story] script for chapter %d of '%s' in %ds（第 %d 版）: %d shots (%ds total), "
          "台词 %d 句 / 旁白 %d 镜 / 有人声 %d 镜（%.0f%%）"
          % (req.n, req.slug, int(time.time() - t0), v, len(shots),
             sum(int(s.get("seconds", req.seconds)) for s in shots),
             lines, narrated, voiced, 100.0 * voiced / max(1, len(shots))), flush=True)
    if shots and voiced * 3 < len(shots) * 2:
        print("[story] ! 本章只有 %d/%d 镜有台词或旁白，低于三分之二；"
              "重拆一次通常能补上" % (voiced, len(shots)), flush=True)
    return sc


# ----------------------------------------------------------------------
# 4 - the image-prompt library that step 2 consumes
# ----------------------------------------------------------------------
def _seed(*parts):
    """Stable per-asset seed. Same id -> same seed forever, so re-rendering one
    character later does not silently give you a different face."""
    h = 0
    for p in parts:
        for cp in str(p):
            h = (h * 131 + ord(cp)) & 0xFFFFFFFF
    return h or 1


class AssetsReq(BaseModel):
    slug: str
    # A 24-strong cast means 24 image generations. The functional NPCs -- the
    # boatman with three lines, the watchman -- rarely need a locked reference
    # sheet, so they are opt-in rather than opt-out.
    include_npc: bool = False
    per_location_times: bool = True     # one plate per time-of-day
    negative: str = "文字, 水印, 多余的手指, 变形, 低分辨率, 现代物品, 欧美面孔, 西式服装, 现代发型, 现代服饰"


# ----------------------------------------------------------------------
# who is who to whom
# ----------------------------------------------------------------------
# The blueprint writes every character in isolation, so nothing makes a father
# and his son share a jaw, or two men of the same yamen share a robe colour.
# That gap is invisible in prose and glaring the moment the reference images
# sit side by side. This step names the ties once, and /assets reads the only
# two fields that can actually change a portrait: a kin pair's shared bone
# structure and a faction's shared palette and insignia.
#
# Who shares a scene with whom is NOT asked of the model -- chapters[].
# characters already says it exactly. Only the nature of each tie is a
# judgement call, so only that is generated.

REL_SCHEMA = """{
 "factions":[{"id":"fa01","name":"","stance":"正/反/中立",
   "palette":"派系每个成员身上都有的颜色与材质（交集；没有共同点就留空）",
   "insignia":"每个成员都有的纹样或制式（交集；个人专属的不要写；没有就留空）",
   "members":["ch01"]}],
 "edges":[{"from":"ch01","to":"ch02","type":"kin/ally/enemy/mentor/romance/duty",
   "subtype":"父子/兄妹/师徒/同僚/宿敌/雇佣","label":"一句话讲清这段关系",
   "mutual":true,"strength":3,"since_chapter":1,
   "turns":[{"chapter":4,"becomes":"enemy","why":""}],
   "shared_features":"仅 kin 需要：两人共有的家族性骨相与五官（脸型、眉骨、眼型、鼻梁、发质），不写情绪"}]
}"""

REL_SYS = ("你是编剧统筹。你要把一份制作圣经里的人物关系理清楚，"
           "供后面画参考图与写分镜时对齐。只输出 JSON，不要解释。")


def _cooccurrence(bp):
    """Which pairs actually share a chapter, straight from the blueprint.

    Handed to the model as fact, so it names the ties that exist instead of
    inventing a tidy family tree nobody wrote."""
    pairs = {}
    for ch in bp.get("chapters", []):
        ids = [c for c in (ch.get("characters") or []) if c]
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.setdefault(tuple(sorted((ids[i], ids[j]))), []).append(ch.get("n"))
    name = {c.get("id"): c.get("name", "") for c in bp.get("characters", [])}
    lines = ["%s(%s) 与 %s(%s) 同场：第 %s 章"
             % (name.get(a, ""), a, name.get(b, ""), b,
                "、".join(str(n) for n in ns))
             for (a, b), ns in sorted(pairs.items())]
    return "\n".join(lines) or "（蓝图未标注每章人物，无同场信息）"


def _relations_brief(bp, rel, cid):
    """The two things a relation graph can actually change in a portrait."""
    if not rel:
        return ""
    bits = []
    name = {c.get("id"): c.get("name", "") for c in bp.get("characters", [])}
    for f in (rel.get("factions") or []):
        if cid in (f.get("members") or []):
            part = [x for x in (f.get("palette"), f.get("insignia")) if x]
            if part:
                bits.append("属「%s」，服饰共用：%s" % (f.get("name", ""), "；".join(part)))
    for e in (rel.get("edges") or []):
        if e.get("type") != "kin" or not e.get("shared_features"):
            continue
        other = e.get("to") if cid == e.get("from") else (
            e.get("from") if cid == e.get("to") else None)
        if not other:
            continue
        bits.append("与 %s(%s) 是%s，共有家族特征：%s"
                    % (name.get(other, ""), other,
                       e.get("subtype") or "亲属", e.get("shared_features")))
    return "；".join(bits)


class RelationsReq(BaseModel):
    slug: str
    notes: str = ""


@app.post("/relations")
def make_relations(req: RelationsReq):
    bp = _need_blueprint(req.slug)
    ids = [c.get("id") for c in bp.get("characters", []) if c.get("id")]
    if not ids:
        raise HTTPException(status_code=400, detail="蓝图里没有人物")
    user = (
        "%s\n\n人物：\n%s\n\n同场事实（来自蓝图，不要修改）：\n%s\n\n"
        "写出这些人物之间的关系图。要求：\n"
        "- 只能用这些 id：%s。不要发明新人物。\n"
        "- 只为确有实质关系的人物对写 edge；泛泛之交不要写。人物多时尤其克制："
        "主要与配角之间、以及 NPC 与主要人物之间才值得写；NPC 与 NPC 之间除非"
        "剧情上确有牵连，否则一律不写。宁可少而准，不要为了铺满而写。\n"
        "- type 为 kin 时必须填 shared_features：两人共有的家族性骨相与五官"
        "（脸型、眉骨、眼型、鼻梁、发质）。它会原样写进两人的参考图提示词，"
        "所以要具体、可照着画、不带情绪与动作。\n"
        "- factions 给人物分派系。palette 与 insignia 只写该派系每一个成员身上都"
        "会出现的部分（交集），因为它们会原样注入派系内每个人的参考图提示词。"
        "任何只属于某一个人的东西——官阶补子、个人佩饰、某人独有的随身物——一律"
        "不要写进来，写了就会画到不该有的人身上。若成员之间服饰其实没有共同点，"
        "palette 与 insignia 就留空字符串，不要硬凑。独行者可以不属于任何派系。\n"
        "- 关系若在故事中转折（结盟转反目、执行者倒戈），写进 turns。\n"
        "%s\n输出格式：\n%s"
        % (_world_brief(bp), _cast_brief(bp), _cooccurrence(bp),
           "/".join(ids), ("额外要求：%s\n" % req.notes) if req.notes else "",
           REL_SCHEMA)
    )
    t0 = time.time()
    _prog_begin("关系图", "人物关系与派系")
    try:
        rel = _chat_json(REL_SYS, user, effort="high", max_tokens=16384,
                         require=lambda o: o.get("edges") or o.get("factions"))
    except Exception:
        _prog_end("关系图失败")
        raise
    _prog_end("关系图完成")

    # The model is good at the judgement and careless with ids. An edge that
    # points at a character who does not exist would silently poison every
    # downstream prompt, so drop it here and say so in the terminal.
    known = set(ids)
    raw_edges = rel.get("edges") or []
    edges = [e for e in raw_edges
             if e.get("from") in known and e.get("to") in known
             and e.get("from") != e.get("to")]
    for f in (rel.get("factions") or []):
        f["members"] = [m for m in (f.get("members") or []) if m in known]
    dropped = len(raw_edges) - len(edges)
    rel["edges"] = edges
    rel["slug"] = req.slug
    _save(req.slug, "relations.json", rel)
    print("[story] relations for '%s' in %ds: %d edges, %d factions%s"
          % (req.slug, int(time.time() - t0), len(edges),
             len(rel.get("factions") or []),
             ("，丢弃 %d 条（id 不存在）" % dropped) if dropped else ""), flush=True)
    return rel


@app.get("/relations/{slug}")
def get_relations(slug: str):
    rel = _load(slug, "relations.json")
    if not rel:
        raise HTTPException(status_code=404,
                            detail="no relations for '%s' -- POST /relations first" % slug)
    return rel


@app.post("/assets")
def make_assets(req: AssetsReq):
    """One prompt per asset, phrased in the blueprint's own world terms.

    Characters get a neutral full-body reference (a '白板图' -- no scene, no
    emotion) because it is a costume/face reference, not a shot. Locations get
    an empty plate per time of day, so a shot can start from the right light.
    """
    bp = _need_blueprint(req.slug)
    style = bp.get("visual_style", {}) or {}
    # Optional on purpose: a project made before /relations existed, or one the
    # user never ran it for, still gets its prompts -- just without the
    # cross-image consistency the graph would have bought.
    def _wanted(c):
        return req.include_npc or (c.get("tier") or "主要") != "NPC"
    cast = [c for c in bp.get("characters", []) if _wanted(c)]
    skipped = len(bp.get("characters", [])) - len(cast)
    rel = _load(req.slug, "relations.json")
    rel_lines = "\n".join(
        "%s(%s)：%s" % (c.get("name", ""), c.get("id", ""), brief)
        for c in cast
        for brief in [_relations_brief(bp, rel, c.get("id"))] if brief
    ) or "（尚未生成关系图，本轮不做跨人物一致性约束）"
    sys_p = ("你是概念设计师，为文生图模型写提示词。提示词要具体到可以照着画："
             "材质、光线、镜头、构图。所有提示词共享同一套影像风格。"
             "只输出 JSON，不要解释。")
    user = (
        "%s\n\n为下面每一项写一条中文文生图提示词。\n"
        "规则：\n"
        "- 每条提示词开头先用文生图模型看得懂的大白话点明朝代、地域、族裔与性别"
        "（例如“明代中国江南，汉人男子，25岁”），再写细节；服饰要带朝代名"
        "（“明代靛蓝直袖长衫”而不是只写“长衫”）。上面「世界」里的时代、地理、历史、"
        "文化与族裔就是答案，不要另编；架空世界同样要把它的规则说成大白话。人物的"
        "阶层要体现在衣料与配饰上（粗布对绫罗，麻绳对玉带）。\n"
        "- 人物：正面全身参考图，中性表情、中性站姿、干净背景，重点是"
        "五官、发型、身形、服饰与随身物件；不要加情绪或剧情。不要写朝向（正面/侧面/"
        "背面）——每位人物会按正面、左侧、右侧、背面各出一张，朝向由系统另加。每位人物还要输出 gender"
        "（男/女），已注明的照抄，未注明的按姓名、称谓与外形判断。\n"
        "- 场景：空景（不要出现人物），分成七栏写：exterior（正面：大门、匾额、石狮、正面外墙、"
        "屋顶轮廓、门前街巷——院内/室内的东西一个字都不要出现在这一栏）、exterior_sides（背面与"
        "两侧的外墙：材质、颜色、后门后窗、山墙、墙根——这一栏里不要出现大门、匾额、石狮，因为背面"
        "和侧面的图就靠它）、layout（一句话平面布局：入口在哪面、主位家具在哪朝哪、两侧各有什么）、"
        "interior_front / interior_back / interior_left / interior_right（分别是站在里面朝四个方向看到的那一面墙和"
        "靠它的家具——四段必须与 layout 一致：正面看到主位家具的正面，背面看到门的内侧和主位家具的背面，"
        "左右两段的东西不能重复，同一件家具只出现在它所在的那一面）、interior（只写进到"
        "里面看得到的：正屋/主厅、厢房、陈设、地面）、roof（从正上方看得到的：瓦面、屋脊、院落平面）、"
        "surroundings（四周邻舍、巷子、树、水）、light（这个时段的光源、方向、色温、明暗比）、"
        "style（统一影像风格一句，含“画幅16:9横构图”）。每栏都要带朝代与地域、材质与颜色。"
        "每处场景会按外部四面、内部四面、俯瞰共九个角度各出一张，每张只拿它看得到的那几栏，"
        "所以栏与栏之间不要互相引用；不要写机位、景深与前后景。有几个时段就写几条（light 不同）。列了几个时段就写几条，夜景要换成"
        "夜的光源（油灯、月光），不能把白天那条照抄。\n"
        "- 道具：白背景产品图式的单体图，同样点明朝代。把物件的结构写死，免得模型自作主张"
        "（“毛笔只有一端有笔头，挂绳系在没有笔头的杆尾”“算盘上二下五珠”“刀连鞘一柄”）。\n"
        "- 每条提示词末尾都要带上统一影像风格。\n"
        "- 「关系约束」里写到的内容必须体现在对应人物的提示词里：血缘共有的骨相"
        "要在两人脸上同时出现，派系服饰要在该派系每个人身上出现。跨图一致性只能"
        "靠这个——文生图模型不会记得上一张画过谁。\n"
        "\n人物：\n%s\n\n关系约束：\n%s\n\n场景：\n%s\n\n道具：\n%s\n"
        '\n输出格式：{"characters":[{"id":"ch01","gender":"男/女","prompt":""}],'
        '"locations":[{"id":"lo01","time_of_day":"日",'
        '"exterior":"正面：从大门前看得到的：大门、匾额、门前石阶/石狮、正面外墙、屋顶轮廓、门前街巷",'
        '"exterior_sides":"背面与两侧：后墙与侧墙的材质颜色、后门或后窗、山墙/马头墙、墙根排水与杂草、旁边的巷子——这里没有大门、匾额、石狮",'
        '"layout":"平面布局一句话：入口在哪一面，主位家具在哪、朝哪，两侧各有什么（后面四个方向的描述都要与它一致）",'
        '"interior_front":"从入口向里看：正对入口那一面墙及其前面的家具（主位家具的正面）",'
        '"interior_back":"从最里面回望入口：入口那一面墙、门的内侧、门两旁的东西，以及主位家具的背面",'
        '"interior_left":"面向左墙：左墙及靠左墙的陈设","interior_right":"面向右墙：右墙及靠右墙的陈设",'
        '"interior":"（兼容旧格式，可留空）",'
        '"roof":"从上面看得到的：屋顶瓦面、屋脊、院落平面布局","surroundings":"四周：邻舍、巷子、树、水、远处（不要写旗杆、旗帜——模型会画成现代旗杆）",'
        '"light":"这个时段的光线：光源、方向、色温、明暗比","style":"统一影像风格那一句"}],'
        '"props":[{"id":"pr01","prompt":""}]}'
        % (_world_brief(bp), _cast_brief(bp, [c.get("id") for c in cast]),
           rel_lines, _loc_brief(bp),
           "\n".join("%s(%s) %s" % (p.get("name", ""), p.get("id", ""), p.get("description", ""))
                     for p in bp.get("props", [])) or "（无）")
    )
    t0 = time.time()
    _prog_begin("资产提示词", "人物 / 场景 / 道具")
    try:
        # effort="medium" asks the template to skip thinking, but ollama's
        # OpenAI endpoint ignores chat_template_kwargs -- verified: a reply
        # capped at 40 tokens still spent them all on `reasoning`. So this
        # call pays the thinking tax too and needs the headroom.
        # The per-wall scene format runs to 12 fields per plate; at 16k the
        # reply truncated, the JSON repair "rescued" one location, and the
        # result overwrote prompts.json with 38 of 97 items. So: room to
        # write, and a rescue that lost a location does not count as one.
        want_loc = {l.get("id") for l in bp.get("locations", []) if l.get("id")}
        want_chr = {c.get("id") for c in cast if c.get("id")}
        def _complete(o):
            have_loc = {r.get("id") for r in (o.get("locations") or [])}
            have_chr = {r.get("id") for r in (o.get("characters") or [])}
            return want_loc <= have_loc and want_chr <= have_chr
        got = _chat_json(sys_p, user, effort="medium", max_tokens=49152, require=_complete)
    # a parts-shaped reply must actually have parts; a model that ignored the
    # new format and sent one "prompt" per location still works via fallback
    except Exception:
        _prog_end("资产提示词失败")
        raise
    _prog_end("资产提示词完成")

    aspect = style.get("aspect", "9:16")
    lib = {"slug": req.slug, "aspect": aspect, "negative": req.negative, "items": []}
    by_id = {}
    for kind in ("characters", "locations", "props"):
        for row in (got.get(kind) or []):
            by_id.setdefault(kind, {})[row.get("id")] = row

    def add(kind, ident, label, prompt, tod=None, anchor=None, suffix="", view=None, seed_key=None, parts=None):
        if not prompt:
            return
        key = "%s_%s%s%s" % (kind[:4], ident, ("_" + tod) if tod else "", suffix)
        # Plates are landscape; people and props stay portrait. The model's own
        # text ends with the frame it was told about, so say the right one.
        item_aspect = "16:9" if kind == "location" else aspect
        if kind == "location":
            prompt = prompt.replace("9:16竖构图", "16:9横构图").replace("9:16", "16:9").replace("竖构图", "横构图")
        lib["items"].append({
            "key": key, "kind": kind, "id": ident, "label": label, "view": view, "aspect": item_aspect,
            "time_of_day": tod, "body": prompt, "prompt": _anchored(prompt, anchor),
            # the model's raw parts, so views can be re-composed later without a model call
            "parts": parts or None,
            "negative": req.negative,
            "seed": _seed(req.slug, seed_key or key), "file": key + ".png", "status": "pending",
        })

    learned = 0
    for c in cast:
        row = (by_id.get("characters") or {}).get(c.get("id")) or {}
        # Blueprints written before gender was in the schema get the model's
        # call; a blueprint that says so wins. What the model decides is
        # written back, so /script's shot anchors say it too instead of the
        # two steps disagreeing about who is a man.
        if not c.get("gender") and (row.get("gender") or "").strip() in ("男", "女"):
            c["gender"] = row["gender"].strip()
            learned += 1
        # A body that still says 正面 (older prompts, or a model that ignored
        # the rule) would fight the side/back heads; say the view instead.
        body = (row.get("prompt") or "").replace("正面全身参考图", "全身参考图").replace("正面", "")
        for suffix, label, desc in CHAR_VIEWS:
            add("character", c.get("id"), c.get("name", ""), body,
                anchor=_anchor(bp, c, view=desc), suffix=suffix, view=label,
                seed_key="char_%s" % c.get("id"))
    if learned:
        _save(req.slug, "blueprint.json", bp)
        print("[story] blueprint '%s': 补上 %d 位人物的 gender" % (req.slug, learned), flush=True)
    # Which parts of the place each view is allowed to know about. The first
    # nine-view run put the whole description under every view, and the model
    # duly painted the courtyard's stone table out in the street for "外·正面".
    # A view now gets only what its camera can see.
    # Back and sides must NOT hear about the gate: given the front-gate text,
    # the model painted 漕运司's gate and lions for "背面" too. They get the
    # sides description; if an older reply has none, surroundings + roof only.
    VIEW_PARTS = {
        "_ext_front": ("exterior", "surroundings", "roof"),
        "_ext_back": ("exterior_sides", "surroundings", "roof"),
        "_ext_left": ("exterior_sides", "surroundings", "roof"),
        "_ext_right": ("exterior_sides", "surroundings", "roof"),
        # each interior view gets the floor plan plus its own wall; an older reply
        # that only has "interior" falls through to that (see _scene_body)
        "_int_front": ("layout", "interior_front"), "_int_back": ("layout", "interior_back"),
        # no layout line for the side walls: it names the desk and chair, and
        # the model centres whatever it is told about
        "_int_left": ("interior_left",), "_int_right": ("interior_right",),
        "_top": ("roof", "surroundings", "exterior"),
    }

    # The model echoes the word even when told not to use it ("无旗杆旗帜"),
    # and a diffusion model hears the noun. Any clause mentioning 旗 is cut.
    _NO_FLAG = re.compile(r"[，、；]?[^，。；、]*旗[^，。；、]*")

    # Words the model turns into the wrong object, replaced by the object itself:
    # "油灯" became an electric desk lamp, "木格窗" got glass panes.
    _OBJECTS = [(re.compile(r"(一盏)?油灯"), "陶碟油灯（小陶碟盛油、一根灯芯明火、没有灯罩）"),
                # "灯光/灯火" through a window rendered as electric light; say what makes it
                (re.compile(r"(?<!火)(灯光|灯火)"), "油灯与蜡烛的火光"),
                (re.compile(r"灯笼(?!（)"), "纸灯笼（竹骨糊纸、内点蜡烛）"),
                # Night light phrasing the model draws as fixtures rather than light:
                # "冷蓝窄条" became blue LED strips across the floor, "光锥" a wall
                # spotlight with a visible cone. Describe the light, not its shape.
                (re.compile(r"冷蓝色?(窄条|光带|光条)?"), "清冷的银白色"),
                (re.compile(r"蓝色(光带|光条|窄条)"), "银白色的月光"),
                (re.compile(r"光锥"), "火光照亮的范围"),
                # a shelf of "书" is drawn as modern spined paperbacks; say what a Ming book is
                (re.compile(r"线装书(?!（)"), "线装书（无书脊、平摊叠放、封面贴题签、纸页发黄）"),
                (re.compile(r"(?<![线装账簿])(书籍|书本|书册)(?!（)"), "线装书（无书脊、平摊叠放、封面贴题签、纸页发黄）")]
    # every window, however it is called ("小窗（木棂）", "木棂窗", "花窗"), says
    # paper and no glass - the model glazes any window it is not told about
    _WINDOW = re.compile(r"((?:木棂|木格|花|后|侧|小|木)?窗(?:户|扇)?)(（[^）]*）)?")

    def _paper_windows(text):
        def fix(mm):
            note = mm.group(2) or ""
            if "纸" in note or "玻璃" in note:
                return mm.group(0)
            return mm.group(1) + (note[:-1] + "、木格糊纸、没有玻璃）" if note else "（木格糊纸、没有玻璃）")
        return _WINDOW.sub(fix, text)

    def _scene_body(row, suffix, tod):
        row = {k: (_NO_FLAG.sub("", v) if isinstance(v, str) else v) for k, v in row.items()}
        for rx, obj in _OBJECTS:
            row = {k: (rx.sub(obj, v) if isinstance(v, str) else v) for k, v in row.items()}
        row = {k: (_paper_windows(v) if isinstance(v, str) else v) for k, v in row.items()}
        keys = VIEW_PARTS[suffix]
        if suffix.startswith("_int") and not any(row.get(k) for k in keys if k != "layout"):
            keys = ("layout", "interior")          # reply written before the per-wall format
        parts = [row.get(k, "") for k in keys]
        parts = [x.strip() for x in parts if x and x.strip()]
        if not parts:
            # older reply shape: one paragraph for everything
            p = (row.get("prompt") or "").strip()
            return ("%s，%s" % (p, tod)) if (p and tod and tod not in p) else p
        light = (row.get("light") or "").strip()
        if suffix in ("_int_left", "_int_right"):
            # the light line likes to say what the light falls on - "黑漆大案漆面反射暖光" -
            # and that is the desk back in a view that must not have it
            light = re.sub(r"[，、；]?[^，。；、]*(大案|主案|公案|案桌|案面|主位|椅)[^，。；、]*", "", light)
            # and the room noun summons its centrepiece: "主事厅内朝西看" drew the desk
            # three times with no desk in the text. Keep the direction, drop the room.
            row = dict(row)
            for k in ("interior_left", "interior_right"):
                if row.get(k):
                    row[k] = re.sub(r"[^，。；：]{1,12}(厅|斋|堂|房|屋|院|铺|肆)内朝(东|西|南|北|左|右)看",
                                    lambda mm: "室内朝%s看" % mm.group(2), row[k])
        if tod and light and tod not in light:
            light = "%s（%s）" % (light, tod)
        return "。".join(x.rstrip("。") for x in parts + [light, (row.get("style") or "").strip()] if x) + "。"

    for l in bp.get("locations", []):
        rows = [r for r in (got.get("locations") or []) if r.get("id") == l.get("id")]
        times = (l.get("times_of_day") or ["日"]) if req.per_location_times else [None]
        for tod in times:
            row = next((r for r in rows if r.get("time_of_day") == tod), rows[0] if rows else {})
            for suffix, label, desc in LOC_VIEWS:
                add("location", l.get("id"), l.get("name", ""), _scene_body(row, suffix, tod), tod,
                    anchor=_anchor(bp, kind="location", view=desc), suffix=suffix, view=label,
                    parts={k: _NO_FLAG.sub("", row.get(k, "")) for k in ("exterior", "exterior_sides", "layout", "interior",
                                                       "interior_front", "interior_back", "interior_left",
                                                       "interior_right", "roof", "surroundings", "light", "style")
                           if row.get(k)})
    for p in bp.get("props", []):
        row = (by_id.get("props") or {}).get(p.get("id")) or {}
        add("prop", p.get("id"), p.get("name", ""), row.get("prompt"), anchor=_anchor(bp, kind="prop"))

    # A reply can still come back thin. Anything the previous prompts.json had
    # that this run did not produce is carried over unchanged, so a render queue
    # reading keys out of this file never finds them gone.
    old = _load(req.slug, "assets/prompts.json") or {}
    have = {i.get("key") for i in lib["items"]}
    carried = [i for i in (old.get("items") or []) if i.get("key") not in have]
    if carried:
        lib["items"].extend(carried)
        print("[story] ! 本轮回复缺了 %d 条，沿用上一版：%s" % (len(carried), ", ".join(i["key"] for i in carried[:6]) + ("…" if len(carried) > 6 else "")), flush=True)
    _save(req.slug, "assets/prompts.json", lib)
    print("[story] assets for '%s' in %ds: %d prompts%s"
          % (req.slug, int(time.time() - t0), len(lib["items"]),
             ("，跳过 %d 位 NPC（include_npc=true 可一并生成）" % skipped)
             if skipped else ""), flush=True)
    return lib


# ----------------------------------------------------------------------
# reading it back
# ----------------------------------------------------------------------
@app.exception_handler(HTTPException)
async def _log_http_error(request, exc):
    """Mirror 4xx/5xx into the terminal. Otherwise a failure is visible only in
    the browser's network tab, and the terminal -- the thing the user is
    watching to decide whether it works -- stays silent.

    Expected absences are the exception. The console asks for a project's
    relation graph every time it opens one, and most projects have never had
    one generated; printing that 404 as `x HTTP 404` puts a normal state in the
    error column, and an error column full of normal states stops being read.
    """
    from fastapi.responses import JSONResponse
    quiet = (exc.status_code == 404
             and request.method == "GET"
             and request.url.path.startswith("/relations/"))
    if not quiet:
        print("[story] x HTTP %s %s -> %s" % (exc.status_code, request.url.path,
                                              str(exc.detail)[:200]), flush=True)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.post("/unload")
def unload():
    """Evict the LLM from VRAM so the next slot can have it.

    Steps 2 and 3 need the whole GPU, and ComfyUI's own /free does not give
    memory back -- only ending the process does. ollama is the exception: a
    request with keep_alive=0 unloads the model but leaves the daemon up, so
    the web app can hand the GPU over without anyone SSHing in.

    Returns freed_gb so the caller can show it actually happened rather than
    just claiming it did.
    """
    base = CFG["llm"].rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base      # ollama native API
    before = _vram_used_gb()
    ok, detail = False, ""
    try:
        req = urllib.request.Request(
            root + "/api/generate",
            json.dumps({"model": CFG["model"], "keep_alive": 0}).encode("utf-8"),
            {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            r.read()
        ok = True
    except Exception as exc:                               # noqa: BLE001
        detail = str(exc)[:200]
    time.sleep(2)                                          # unload is not instant
    after = _vram_used_gb()
    freed = round((before - after), 1) if (before and after) else None
    print("[story] unload %s: %s%s" % (
        CFG["model"], "ok" if ok else "failed " + detail,
        (" · 释放 %.1fGB (%.1f -> %.1f)" % (freed, before, after)) if freed else ""), flush=True)
    return {"ok": ok, "detail": detail or "unloaded",
            "vram_before_gb": before, "vram_after_gb": after, "freed_gb": freed}


def _vram_used_gb():
    """Best effort. None when there is no vendor tool -- reporting nothing is
    better than reporting a made-up number."""
    for argv, parse in (
        (["rocm-smi", "--showmeminfo", "vram", "--json"], "rocm"),
        (["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"], "nv"),
    ):
        try:
            out = subprocess.run(argv, capture_output=True, timeout=6)
            if out.returncode != 0:
                continue
            txt = out.stdout.decode("utf-8", "replace")
            if parse == "rocm":
                for card in json.loads(txt).values():
                    for k, v in card.items():
                        if "Used Memory" in k:
                            return round(int(v) / 1e9, 1)
            else:
                return round(float(txt.strip().splitlines()[0]) * 1048576 / 1e9, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


@app.get("/health")
def health():
    ok, detail = True, "ok"
    try:
        req = urllib.request.Request(CFG["llm"].rstrip("/") + "/models")
        with urllib.request.urlopen(req, timeout=5) as r:
            models = [m.get("id") or "" for m in json.load(r).get("data", [])]
        # ollama reports "name:latest" for a bare name; llama.cpp reports the
        # --alias verbatim. Compare on the stem so both match.
        stem = lambda m: m.split(":")[0]
        if CFG["model"] not in models and stem(CFG["model"]) not in [stem(m) for m in models]:
            ok, detail = False, "model '%s' not served (have: %s)" % (CFG["model"], models[:5])
    except Exception as exc:                           # noqa: BLE001
        ok, detail = False, "LLM unreachable: %s" % exc
    return {"status": "ok" if ok else "degraded", "detail": detail,
            "llm": CFG["llm"], "model": CFG["model"], "projects": CFG["projects"]}


@app.get("/projects")
def list_projects():
    root = CFG["projects"]
    if not os.path.isdir(root):
        return {"projects": []}
    out = []
    for name in sorted(os.listdir(root)):
        bp = _load(name, "blueprint.json")
        if not bp:
            continue
        n_ch = len([f for f in os.listdir(_proj_dir(name, "chapters"))
                    if f.endswith(".json")]) if os.path.isdir(_proj_dir(name, "chapters")) else 0
        out.append({"slug": name, "title": bp.get("title", ""),
                    "chapters_planned": len(bp.get("chapters", [])),
                    "chapters_written": n_ch, "revision": bp.get("revision", 1)})
    return {"projects": out}


@app.get("/project/{slug}")
def get_project(slug: str):
    bp = _need_blueprint(slug)
    chapters, scripts = [], []
    for i in range(1, len(bp.get("chapters", [])) + 1):
        ch = _load(slug, "chapters/%02d.json" % i)
        if ch:
            chapters.append({"n": i, "title": ch.get("title", ""),
                             "words": len(ch.get("prose", "")), "summary": ch.get("summary", "")})
        sc = _load(slug, "script/%02d.json" % i)
        if sc:
            scripts.append({"n": i, "shots": len(sc.get("shots", []))})
    return {"blueprint": bp, "chapters": chapters, "scripts": scripts,
            "assets": _load(slug, "assets/prompts.json")}


def main():
    ap = argparse.ArgumentParser(description="Bookreel story engine (step 1: LLM only)")
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--llm", default="http://127.0.0.1:11434/v1",
                    help="OpenAI-compatible base URL (ollama :11434/v1, llama.cpp :8000/v1)")
    ap.add_argument("--model", default="qwen3.8-27b")
    ap.add_argument("--projects", default=os.path.normpath(os.path.join(here, "..", "projects")))
    ap.add_argument("--timeout", type=int, default=1800)
    args = ap.parse_args()

    # Install the tee before anything else prints, so the banner below is the
    # first thing the browser terminal shows.
    sys.stdout = _Tee(sys.stdout, "out")
    sys.stderr = _Tee(sys.stderr, "err")

    CFG.update(llm=args.llm, model=args.model,
               projects=os.path.abspath(args.projects), timeout=args.timeout)
    os.makedirs(CFG["projects"], exist_ok=True)
    print("[story] llm       : %s  (%s)" % (CFG["llm"], CFG["model"]), flush=True)
    print("[story] projects  : %s" % CFG["projects"], flush=True)
    print("[story] listening : http://%s:%d" % (args.host, args.port), flush=True)
    print("[story] step 1 of 3 -- this service never loads image or video models.", flush=True)

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
