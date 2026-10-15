# -*- coding: utf-8 -*-
"""Bookreel . static server for the web console (slot: none, it is just files).

The console cannot be opened as a file:// URL. The dc runtime in support.js
re-reads its own source with fetch(location.href) while booting, and browsers
refuse that on file://, so the page comes up blank. It needs a real HTTP
origin -- which is all this is.

    python3 web_server.py --port 8080
    python3 web_server.py --port 8080 --host 0.0.0.0   # reachable from the LAN

Stdlib only, on purpose: this has to start before any venv exists, and the
whole point is to be the one thing that always comes up.

Only the files the two pages actually need are served (see ALLOW). The repo
root also holds models/, logs/, venvs/ and whatever Hugging Face token the
user pasted, and a plain SimpleHTTPRequestHandler would hand all of it to
anyone who asks -- which matters a great deal more once --host is 0.0.0.0.

Endpoints
    GET /                       -> index listing the pages
    GET /<name>.dc.html         -> a console / design page
    GET /support.js             -> the dc runtime
    GET /_ds/<uuid>/<file>      -> design-system css + bundle
    GET /comfy-flux2-9x16.json  -> the workflow template the console posts
    GET /gpu                    -> real local GPU / VRAM / disk, as JSON
    GET /api/projects           -> the projects step 1 has produced
    GET /api/library?slug=      -> that project's asset + clip library, grouped
    GET /api/blueprint?slug=    -> the full production bible
    GET /api/chapter?slug=&n=   -> one chapter's prose + director's script
    GET /media/<slug>/<path>    -> one generated image or clip

The library reads projects/<slug>/*.json straight off disk rather than asking
story_server, so it still works after step 1 is shut down to free VRAM -- which
is the normal state while steps 2 and 3 run.

The /gpu route exists because the Studio page used to print "2 x A100 - 78%"
as static text. That was design-comp filler, and on any real machine it is a
lie. Better to report the actual card, or say plainly that we cannot tell,
than to draw a confident bar chart of invented numbers.
"""

import argparse
import glob
import hashlib
import io
import json
import math
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import random
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request

import filmwords
import foley
import fightcut
import look
import shotqa
import stage
from urllib.parse import parse_qs, unquote, urlparse

# Exact filenames, plus the two directory prefixes, that the pages reference.
ALLOW_FILES = {
    "support.js",
    "comfy-flux2-9x16.json",
}
ALLOW_DIRS = ("_ds/",)
ALLOW_SUFFIX = (".dc.html",)

TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECTS = os.path.join(ROOT, "projects")

# Only these come out of projects/. Everything there is generated, but the
# directory also holds prose and prompts, and a media route has no business
# serving JSON.
MEDIA_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".wav", ".mp3"}


# ----------------------------------------------------------------------
# takes -- every render of an asset, not just the last one
# ----------------------------------------------------------------------
# /api/save used to overwrite assets/images/char_ch01.png in place, so a
# re-render silently destroyed the previous picture and the page - same URL,
# cached bytes - went on showing the old one anyway. Now each render lands
# as assets/images/_takes/char_ch01/<n>.png and the live file is a copy of
# whichever take is chosen (_active.json), so the rest of the pipeline keeps
# reading the same path and the page can offer a list to pick from.
TAKES_DIR = "_takes"


def _take_dir(full):
    d, fn = os.path.split(full)
    stem, ext = os.path.splitext(fn)
    return os.path.join(d, TAKES_DIR, stem), ext


def _take_list(full):
    d, ext = _take_dir(full)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        stem, e = os.path.splitext(fn)
        if e.lower() == ext.lower() and stem.isdigit():
            out.append(int(stem))
    return sorted(out)


def _take_active(full):
    d, _ = _take_dir(full)
    try:
        with open(os.path.join(d, "_active.json"), encoding="utf-8") as f:
            return int(json.load(f).get("take"))
    except (OSError, ValueError, TypeError):
        pass
    ts = _take_list(full)
    return ts[-1] if ts else None


def _take_meta(full, n=None):
    """The sidecar of a take - what the renderer recorded about it.

    `_take_active` answers WHICH take is live and answers it as a NUMBER; the
    facts about that take (seed, verdict, segment count) are in <n>.json beside
    it. Reading the number as if it were the record is how /api/library started
    raising 'int' object has no attribute 'get' for every project that had ever
    rendered a clip.
    """
    d, _ext = _take_dir(full)
    n = _take_active(full) if n is None else n
    if not n:
        return {}
    return _read_json(os.path.join(d, "%d.json" % int(n)), {}) or {}


def _passed_keys(slug, keys):
    """Keys whose LIVE picture is ✓ 已核 or ✓ 人工认可 (check.ok is True)."""
    lib = _read_json(os.path.join(PROJECTS, slug, "assets", "prompts.json"), {}) or {}
    files = {i.get("key"): i.get("file") for i in lib.get("items", []) if i.get("file")}
    out = []
    for k in keys:
        full = os.path.join(PROJECTS, slug, "assets", "images", files.get(k) or "")
        if files.get(k) and os.path.isfile(full) and (_take_meta(full).get("check") or {}).get("ok") is True:
            out.append(k)
    return out


def _take_write_active(full, n):
    d, _ = _take_dir(full)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "_active.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"take": int(n)}, f)
    os.replace(tmp, os.path.join(d, "_active.json"))


def _take_adopt(full):
    """A live file rendered before takes existed becomes take 1, so nothing
    already on disk is lost the first time it is re-rendered."""
    if _take_list(full) or not os.path.isfile(full):
        return
    d, ext = _take_dir(full)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(full, os.path.join(d, "1" + ext))
    _take_write_active(full, 1)


def _take_add(full, body, meta=None):
    _take_adopt(full)
    d, ext = _take_dir(full)
    n = (_take_list(full) or [0])[-1] + 1
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "%d%s.tmp" % (n, ext))
    with open(tmp, "wb") as f:
        f.write(body)
    os.replace(tmp, os.path.join(d, "%d%s" % (n, ext)))
    if meta:
        # The seed, mostly: a take you like should be reproducible, and the
        # page stopped locking seeds on re-render precisely so takes differ.
        with open(os.path.join(d, "%d.json" % n), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
    _take_select(full, n)
    return n


def _take_note(full, n, extra):
    """Merge fields into a take's sidecar (the seed etc. are already there)."""
    d, _ = _take_dir(full)
    p = os.path.join(d, "%d.json" % int(n))
    meta = _read_json(p, {}) or {}
    meta.update(extra)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def _take_select(full, n):
    d, ext = _take_dir(full)
    src = os.path.join(d, "%d%s" % (int(n), ext))
    if not os.path.isfile(src):
        return False
    tmp = full + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, full)
    _take_write_active(full, n)
    return True


def _take_delete(full, n):
    """Remove one take. If it was the live one, the newest remaining take
    takes its place; if none remain, the live file goes too and the asset
    reads as 未生成 again, which is the truth."""
    d, ext = _take_dir(full)
    p = os.path.join(d, "%d%s" % (int(n), ext))
    if not os.path.isfile(p):
        return None
    was_active = _take_active(full) == int(n)
    os.remove(p)
    try:
        os.remove(os.path.join(d, "%d.json" % int(n)))
    except OSError:
        pass
    rest = _take_list(full)
    if was_active:
        if rest:
            _take_select(full, rest[-1])
        else:
            try:
                os.remove(full)
                os.remove(os.path.join(d, "_active.json"))
            except OSError:
                pass
    return rest


def _clip_seconds(full):
    """Rendered length of the live take, from its sidecar. 0.0 if unknown.

    The subtitle timeline is built from this, not from how long the narration
    ran: a chapter's shots are what advance the clock, and 50 of them say
    nothing at all.
    """
    d, _ext = _take_dir(full)
    n = _take_active(full)
    if not n:
        return 0.0
    meta = _read_json(os.path.join(d, "%d.json" % n), {}) or {}
    try:
        return float(meta.get("seconds") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _takes_for(slug, rel, full):
    """What the page needs to draw the strip: one row per take, newest last."""
    d, ext = _take_dir(full)
    active = _take_active(full)
    rows = []
    for n in _take_list(full):
        p = os.path.join(d, "%d%s" % (n, ext))
        try:
            st = os.stat(p)
        except OSError:
            continue
        meta = _read_json(os.path.join(d, "%d.json" % n), {}) or {}
        rows.append({"n": n, "active": n == active, "bytes": st.st_size,
                     "mtime": int(st.st_mtime), "seed": meta.get("seed"), "check": meta.get("check"),
                     "approved": bool(meta.get("approved")), "lipsync": bool(meta.get("lipsync")),
                     "url": "/media/%s/%s/%s/%s/%d%s?t=%d" % (
                         slug, os.path.dirname(rel), TAKES_DIR,
                         os.path.splitext(os.path.basename(rel))[0], n, ext, int(st.st_mtime))})
    return rows


# ----------------------------------------------------------------------
# the render queue -- lives here, not in the browser
# ----------------------------------------------------------------------
# The console used to drive batches from the page: a JS loop that submitted
# one prompt, polled, saved, and moved on. Close the tab, lose the laptop's
# wifi, let the phone sleep, and the batch silently stopped -- and nobody
# could tell whether it was still going. Now the batch is a thread in this
# process, its state is on disk (projects/<slug>/assets/batch.json), it
# resumes itself when the server restarts, and it retries when ComfyUI is
# down instead of giving up. The page only asks how it is going.
COMFY = "http://127.0.0.1:7860"
WEB_PORT = [8080]        # set from main(); the video service fetches first frames from here
WORKFLOW = os.path.join(ROOT, "comfy-flux2-9x16.json")
BATCH_CLIENT = "bookreel-batch"          # the page opens a socket under this id to see steps

# The image graph guides through FluxGuidance + BasicGuider, and BasicGuider
# has ONE conditioning input: there is no negative in it at all. So the
# `negative` string /assets composes and stores on prompts.json has never been
# read by anything - not by this server, not by the page, not by ComfyUI - and
# every word added to it (玻璃灯罩, 月亮, 硬边光斑 on 2026-09-17) did nothing.
# CLAUDE.md said the image side had a working negative; it did not.
#
# Setting this above 1.0 swaps BasicGuider for CFGGuider, which does take one,
# and the string starts working. It is off by default because a real CFG is a
# SECOND model evaluation per step - the same arithmetic that rules a negative
# out on the video side, where 27 min a segment would become ~54. An image is
# ~440 s here, so a full 97-item run goes from ~12 h to ~24.
#
# Left at 1.0 the lever is still the one the rest of this pipeline uses: say
# the true thing positively (look.always_positive, "画面中只有一个人") and
# strip the word you do not want (services/filmwords.py, look.destrip).
IMAGE_CFG = float(os.environ.get("BOOKREEL_IMAGE_CFG", "1"))
# ...except on a CHARACTER SHEET, where it is on by default. A sheet is the
# face and body every clip of that person is conditioned on, and anatomy is
# exactly what the positive "两条手臂" does not hold: tianlongbabu's 左子穆
# (2026-09-24) came back with three hands and passed. Twice the time for the
# 4 x cast sheets only (~7 -> ~14 min each); plates and props stay at 1.0.
CHAR_CFG = float(os.environ.get("BOOKREEL_CHAR_CFG", "2.0"))
CHAR_BG_NEGATIVE = ("背景, 场景, 风景, 建筑, 房屋, 室内, 室外, 墙壁, 门窗, 家具, 树木, 山, 天空, 云, "
                    "地面纹理, 地板, 草地, 石板路, 道具, 其他物品, 其他人物, 彩色背景, 渐变背景, 暗色背景, "
                    "background, scenery, landscape, building, interior, room, wall, furniture, trees, "
                    "sky, floor texture, props, colored background, gradient background, dark background")

# A LOCATION PLATE is the first frame of every cold shot there, so a person in
# it is a person in the clip (lo01's 日_ext_left put a man in C01S01). The
# prompt used to say 空景无人 - a denial with 人 in it - and the negative was
# never read. Plates now get the character sheets' treatment (2026-09-25): a
# real CFG negative that subtracts every word for a person, and a positive
# that says what IS there. Twice the time per plate (~7 -> ~14 min).
LOC_CFG = float(os.environ.get("BOOKREEL_LOC_CFG", "2.0"))
LOC_PEOPLE_NEGATIVE = ("人, 人物, 行人, 路人, 人群, 人影, 身影, 剪影, 背影, 侧影, 人形, 脸, 手, 身体, "
                       "男人, 女人, 老人, 小孩, 士兵, 僧人, 武士, 骑马的人, 站着的人, 坐着的人, 远处的小人, "
                       "person, people, human, figure, silhouette, crowd, pedestrian, man, woman, child, "
                       "face, hands, body, rider, soldier, monk, tiny distant figure")

# ----------------------------------------------------------------------
# the check -- a vision model looks at every render before it is accepted
# ----------------------------------------------------------------------
# 陆横's right-side view came back with two swords; earlier renders put a
# lace-up boot on a Ming soldier and a fresh wound on an arm nobody
# described. A diffusion model does this constantly and nobody was looking.
# So each finished image is put in front of a vision model with a short list
# of yes/no questions derived from the prompt (one question per call - a long
# checklist made the 27B hallucinate; single questions it answers well). A
# failed check re-renders with the failed points spelled out and a fresh
# seed, up to VERIFY_RETRIES times; every verdict is stored beside the take
# and shown on the tile, so a picture that still failed is at least labelled.
OLLAMA = "http://127.0.0.1:11434"
VERIFY_MODEL = os.environ.get("BOOKREEL_VERIFY_MODEL", "qwen3-vl:8b")
# 0 by default since 2026-09-24: a failed check no longer re-renders by
# itself. The verdict is shown on the tile and the PERSON decides - 认可, or a
# note on what is wrong and 按意见重出 (item["user_note"], sent as 【必须】…).
# The checker was wrong often enough (a correct swordless 段誉 "fixed" into
# carrying one) that its opinion is advice, not an order.
VERIFY_RETRIES = int(os.environ.get("BOOKREEL_VERIFY_RETRIES", "0"))
VERIFY_ENABLED = True


def _vlm_json(prompt, png_bytes, timeout=1500, temperature=0, prefill=""):
    """One structured question about one image. qwen3-vl always thinks first
    (think:false is ignored, and a small num_predict starves the answer), so
    give it room and ask for JSON; one call per image, ~30-60 s here."""
    import base64
    # A 1920x1080 PNG costs thousands of image tokens; at 8k context the model's
    # reasoning was cut off mid-thought ("n_tokens = 8191, truncated = 1") and
    # it answered nothing. Send a 1280-px JPEG and give it 16k of context.
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        im.thumbnail((1280, 1280))
        buf = io.BytesIO(); im.save(buf, "JPEG", quality=90); img_bytes = buf.getvalue()
    except Exception:                                       # noqa: BLE001
        img_bytes = png_bytes
    # The prefill is the whole trick: qwen3-vl reasons before answering and
    # cannot be told not to, and on a busy scene it reasoned past a 12000-token
    # budget and returned nothing (13172 tokens, 5m50s, twice). Handing it the
    # opening of the JSON object as a partial assistant turn makes it continue
    # that object instead of starting to think: same answer in 15 s.
    msgs = [{"role": "user", "content": prompt, "images": [base64.b64encode(img_bytes).decode()]}]
    if prefill:
        msgs.append({"role": "assistant", "content": prefill})
    body = {"model": VERIFY_MODEL, "stream": False, "format": "json", "messages": msgs,
            "options": {"temperature": temperature, "num_predict": 12000, "num_ctx": 32768}}
    req = urllib.request.Request(OLLAMA + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = (json.load(r).get("message", {}).get("content") or "").strip()
    if prefill and not txt.lstrip().startswith("{"):
        txt = prefill + txt                              # the model continued our object
    try:
        return json.loads(txt)
    except ValueError:
        m = re.search(r"\{.*\}", txt, re.S)
        return json.loads(m.group(0)) if m else {}


def _checks_for(item, bp):
    """The fields to ask for, and how to judge each: (field, question text, judge(value)->problem or None, fix text).
    Counts are asked decomposed ("刀的数量、刀鞘数量、出鞘刀身数量") - asked plainly the model said one
    sword where there were two; asked to count hilts, sheaths and bare blades separately it gets it right."""
    kind, view = item.get("kind"), item.get("view") or ""
    def num(v):
        try:
            return int(str(v).strip().split()[0])
        except (ValueError, IndexError):
            return None
    def yes(v):
        return str(v).strip().lower() in ("yes", "true", "是", "有", "1")
    qs = []
    if kind == "character":
        c = next((x for x in bp.get("characters", []) if x.get("id") == item.get("id")), {})
        qs.append(("人数", "画面里一共有几个人（数字）", lambda v: None if num(v) == 1 else "人数 %s，应只有一人" % v, "画面里只有一个人"))
        qs.append(("背景", "人物身后的背景是不是一片纯白、没有任何景物（是/否，不是的话写出看到了什么）", lambda v: None if str(v).strip().startswith(("是", "yes", "Yes")) else "背景不是纯白：%s" % v, "纯白色无缝背景，白色地面，身后什么都没有"))
        # Nothing counted limbs, so 左子穆's third hand passed (2026-09-24).
        # Asked as hands AND arms: a hand at the beard and one at the hip read
        # as "two hands" to a model asked only about arms.
        qs.append(("手的数量", "数一数画面里能看到几只手（手掌），被袖子完全遮住的不算（数字）", lambda v: None if (num(v) or 0) <= 2 else "有 %s 只手，应最多两只" % v, "这个人只有两条手臂、两只手，双臂自然下垂贴在身体两侧"))
        qs.append(("腿的数量", "数一数画面里有几条腿、几只脚（写腿的数字）", lambda v: None if (num(v) or 0) <= 2 else "有 %s 条腿，应只有两条" % v, "这个人只有两条腿，双脚并立站在地上"))
        g = (c.get("gender") or "").strip()
        if g in ("男", "女"):
            qs.append(("性别", "人物是男是女（男/女）", lambda v: None if g in str(v) else "性别看起来是 %s，应为 %s" % (v, g), "人物是%s性" % g))
        if view in ("左侧", "右侧"):
            want = "左" if view == "左侧" else "右"
            qs.append(("面朝", "人物的脸朝向画面的左边缘还是右边缘（左/右）", lambda v: None if want in str(v) else "面朝%s，应面朝%s" % (v, want), "人物面朝画面%s侧" % want))
        if view == "背面":
            qs.append(("看得到脸", "能看到人物的脸吗（是/否）", lambda v: "看得到脸，应背对镜头" if yes(v) else None, "人物背对镜头，看不到脸"))
        # No weapon on any sheet (filmwords.WEAPON_RX): it is an accessory.
        qs.append(("刀的数量", "画面里一共有几把刀或剑，鞘中的和手里拿着的都算（数字）", lambda v: None if (num(v) or 0) == 0 else "有 %s 把刀剑，人物参考图上不带兵器" % v, "双手空着自然下垂，腰间只系一条布带，身上只有衣服"))
        qs.append(("现代物品", "有没有现代物品：系带靴、拉链、手表、眼镜、塑料、印刷品等（有/无，有的话写出是什么）", lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "现代物品：%s" % v, "没有任何现代物品，鞋是该时代的靴子，没有鞋带"))
        qs.append(("鞋带", "鞋或靴子上有没有鞋带（有/无）", lambda v: "靴子有鞋带（现代系带靴）" if str(v).strip() in ("有", "yes", "是") else None, "脚上是这个时代样式的鞋或靴：整片面料缝成，没有鞋带和绑带"))
        # A described 旧刀疤 keeps coming out as a fresh red gash. Ask about
        # fresh wounds only, and correct toward "healed, pale, old" rather than
        # "no scar" - the blueprint wants the scar.
        qs.append(("新鲜伤口", "身上有没有新鲜的、红色的、带血的伤口（有/无）——愈合多年的浅色旧疤不算", lambda v: "有新鲜伤口/血迹" if str(v).strip() in ("有", "yes", "是") else None,
                   "皮肤完好无破损，露出的手臂上只有一道愈合多年的浅色平滑旧疤，没有红色、没有血"))
        qs.append(("全身入镜", "从头到脚是否完整入镜（是/否）", lambda v: None if yes(v) else "没有全身入镜", "全身从头到脚完整入镜"))
        qs.append(("文字水印", "画面里有没有文字或水印（有/无）", lambda v: "有文字/水印" if str(v).strip() in ("有", "yes", "是") else None, "画面里没有文字与水印"))
    elif kind == "location":
        qs.append(("人数", "画面里有几个人（数字，没有就是 0）", lambda v: None if (num(v) or 0) == 0 else "画面里有 %s 人，应为空景" % v, "空景，画面里没有任何人"))
        if view.startswith("外"):
            qs.append(("室内外", "镜头是在建筑外面看外墙，还是在院内/室内（外/内）", lambda v: None if "外" in str(v) else "镜头在里面，应在外面", "站在建筑外面看外墙，室内陈设看不见"))
        if view.startswith("内"):
            qs.append(("室内外", "镜头是在建筑外面看外墙，还是在院内/室内（外/内）", lambda v: None if "内" in str(v) else "镜头在外面，应在里面", "置身建筑内部"))
        if view in ("内·左侧", "内·右侧"):
            qs.append(("正中是主案", "画面正中是不是一张正对镜头的大案/桌子和它后面的主位椅子（是/否）", lambda v: "画面正中还是主案与主位椅，应是侧墙" if yes(v) else None,
                       "镜头转向侧墙：画面正中只有那面墙和贴墙的陈设，大案与主位椅不在画面里"))
        if view == "内·背面":
            qs.append(("最远处是门", "画面最远处、正对镜头的是不是一扇门或门洞（是/否）", lambda v: None if yes(v) else "回望入口时最远处应是门", "画面最远处是大门的内侧"))
            qs.append(("主椅朝向", "如果画面里有主位的椅子，它是椅背朝着镜头还是椅面朝着镜头（椅背/椅面/没有椅子）", lambda v: "主位椅子椅面朝镜头，回望入口时应是椅背" if "椅面" in str(v) else None, "主位椅子椅背朝着镜头，案在椅子前面更远处"))
        if view == "俯瞰":
            qs.append(("正俯视", "是不是从正上方垂直往下看、看不到地平线（是/否）", lambda v: None if yes(v) else "不是正俯视", "垂直正俯视，没有地平线"))
        qs.append(("现代物品", "有没有现代物品：电灯、汽车、塑料、现代印刷书籍、玻璃窗、电线等（有/无，有的话写出是什么）", lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "现代物品：%s" % v, "没有任何现代物品"))
        qs.append(("旗帜旗杆", "画面里有没有旗帜、旗子或旗杆（有/无）", lambda v: "有旗帜/旗杆" if str(v).strip() in ("有", "yes", "是") else None, "画面里没有任何旗帜和旗杆"))
        # 2026-09-17: lo01's night plates passed every check above and were
        # still wrong in two ways that every night clip then inherited - a
        # kerosene lamp with a glass chimney where the text said 陶碟油灯, and
        # a large glowing moon disc in the middle of the frame. "现代物品" does
        # not catch either: the vision model does not read a brass oil lamp as
        # modern, and a moon is not an object out of period.
        if "油灯" in str(item.get("prompt") or ""):
            qs.append(("灯罩", "画面里的油灯有没有玻璃灯罩、玻璃灯筒或金属灯座（有/无，有的话写出是什么）",
                       lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "灯有灯罩/灯筒：%s" % v,
                       "灯是一只小陶碟盛油、一根灯芯露在碟口明火燃烧，灯口完全敞开"))
        if str(item.get("time_of_day") or "") == "夜":
            qs.append(("月亮", "画面里有没有画出月亮本体（一个发光的圆盘）（有/无）",
                       lambda v: "画出了月亮本体" if str(v).strip() in ("有", "yes", "是") else None,
                       "夜空是深蓝的，天上只有星点与薄云，地面是清冷的银白色夜光"))
            qs.append(("光斑", "地面或墙面上有没有一片一片边缘很硬的亮斑、亮条或亮柱（有/无）",
                       lambda v: "有硬边光斑/光柱" if str(v).strip() in ("有", "yes", "是") else None,
                       "夜光落在地面上是柔和不匀的一片，边缘模糊"))
    elif kind == "prop":
        # a brush with its hanging strap, a sword with its scabbard: the accessory
        # the description names is part of the object, not a second item
        qs.append(("物件数量", "不算描述里提到的配件（笔挂布条、刀鞘、系绳、书签之类），画面里有几件独立的物品（数字）", lambda v: None if num(v) == 1 else "物件 %s 件，应只有一件" % v, "只有一件物品及其自身的配件"))
        # the brush came back with a head at each end; ask about duplicated parts
        qs.append(("重复部件", "这件物品有没有重复或多出来的部件，例如毛笔两端都有笔头、刀有两个刀柄、书有两个书脊（有/无，有的话写出是什么）",
                   lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "重复部件：%s" % v, "物件结构正确，每个部件只有一个"))
        name = str(item.get("label") or "")
        # A 剑 was drawn as a katana on every sheet (2026-09-28): a long
        # wrapped two-hand grip and a round disc guard. Asked separately,
        # because the grip and the guard are what read as Japanese.
        bn = filmwords.blade_noun(name) if (str(item.get("id") or "").startswith("pw_") or item.get("weapon")) else ""
        if bn:
            qs.append(("柄长", "%s柄有多长：是只够一只手握住的短柄，还是能两只手一前一后握住的长柄（短柄/长柄）" % bn,
                       lambda v: None if "短" in str(v) else "%s柄是双手长柄，应是只够单手一握的短柄" % bn,
                       "%s柄很短，只够单手一握" % bn))
            if bn == "剑":
                qs.append(("剑格", "剑柄和剑鞘之间的护手是什么形状：一小块短而厚的横档，还是一块圆盘或椭圆盘（横档/圆盘）",
                           lambda v: None if "横" in str(v) else "护手是圆盘（日本刀式），应是窄横档",
                           "剑格短而厚，只比剑柄宽出一点"))
        if "笔" in name:
            qs.append(("笔头数量", "这支笔有几个笔头/毛头（数字）", lambda v: None if num(v) == 1 else "笔头 %s 个，应只有一个" % v, "毛笔只有一端有笔头，另一端是光秃的竹杆末端"))
            # the strap was drawn hanging from the ferrule; it belongs at the bare end
            qs.append(("挂绳位置", "如果有挂绳/挂布，它系在笔的哪一端（笔头那一端/没有笔头的杆尾/没有挂绳）",
                       lambda v: "挂绳系在笔头那一端，应在杆尾" if "笔头" in str(v) and "没有" not in str(v) else None,
                       "挂绳系在笔杆末端——没有笔头的那一端，离笔头最远"))
        qs.append(("人或手", "画面里有没有人或手（有/无）", lambda v: "有人/手" if str(v).strip() in ("有", "yes", "是") else None, "没有人物和手"))
        # 《大明律》 is a book: its own title slip is not a watermark. Ask about
        # text that does not belong to the object.
        qs.append(("多余文字", "画面上有没有与物品本身无关的文字、字幕、标签或水印（物品自带的书名、题签、印章、刻字不算）（有/无）",
                   lambda v: "有多余文字/水印" if str(v).strip() in ("有", "yes", "是") else None, "画面里没有字幕与水印"))
    return qs


def _verify(item, png_bytes, bp):
    """One JSON call; return {'ok', 'problems', 'fix', 'answers', 'model', 'seconds'}. ok=None if the model is unavailable."""
    t0 = time.time()
    checks = _checks_for(item, bp)
    if not checks:
        return {"ok": True, "problems": [], "fix": "", "answers": {}, "model": VERIFY_MODEL, "seconds": 0}
    prompt = ("仔细看图，逐项回答。只输出一个 JSON 对象，键名与下面完全一致，值要简短：\n" +
              "\n".join("- \"%s\": %s" % (k, q) for k, q, _, _ in checks))
    prefill = '{"%s":' % checks[0][0]
    try:
        ans = _vlm_json(prompt, png_bytes, prefill=prefill)
        if not isinstance(ans, dict) or not any(str(ans.get(k, "")).strip() for k, _, _, _ in checks):
            ans = _vlm_json(prompt, png_bytes, temperature=0.2, prefill=prefill)  # empty: one more try
    except Exception as exc:                                # noqa: BLE001
        return {"ok": None, "problems": ["审核模型不可用：%s" % str(exc)[:120]], "fix": "", "answers": {}, "model": VERIFY_MODEL,
                "seconds": int(time.time() - t0)}
    # An empty or wildly incomplete reply (thinking ate the budget, JSON cut
    # off) is "no verdict", never "everything failed": one such reply sent a
    # correct image back for a needless re-render.
    answered = [k for k, _, _, _ in checks if str(ans.get(k, "")).strip() != ""]
    if not isinstance(ans, dict) or len(answered) < max(1, len(checks) // 2):
        return {"ok": None, "problems": ["审核模型没有给出完整回答（%d/%d）" % (len(answered), len(checks))], "fix": "",
                "answers": ans if isinstance(ans, dict) else {}, "model": VERIFY_MODEL, "seconds": int(time.time() - t0)}
    def judge_all(a):
        out = {}
        for key, _, judge, fix in checks:
            v = a.get(key, "")
            if str(v).strip() == "":
                continue                                    # unanswered field: no opinion
            try:
                prob = judge(v)
            except Exception:                               # noqa: BLE001
                prob = None
            if prob:
                out[key] = (prob, fix)
        return out
    found = judge_all(ans)
    # A second opinion before spending seven minutes on a re-render: the model
    # called one sheathed sword "two swords, one drawn" on a picture it had
    # passed on that point before. Only a problem seen twice counts.
    if found:
        try:
            ans2 = _vlm_json(prompt, png_bytes, temperature=0.4, prefill=prefill)
            found2 = judge_all(ans2) if isinstance(ans2, dict) else {}
            confirmed = {k: v for k, v in found.items() if k in found2}
            dropped = [found[k][0] for k in found if k not in found2]
        except Exception:                                   # noqa: BLE001
            confirmed, dropped = found, []
        found = confirmed
    else:
        dropped = []
    problems = [v[0] for v in found.values()]
    fixes = [v[1] for v in found.values() if v[1]]
    if dropped:
        problems_note = "（第二次核对未复现，忽略：%s）" % "；".join(dropped)
    else:
        problems_note = ""
    return {"ok": not problems, "problems": problems, "fix": "；".join(dict.fromkeys(fixes)), "answers": ans,
            "note": problems_note, "model": VERIFY_MODEL, "seconds": int(time.time() - t0)}


def sheet_cfg(item):
    """Whether this item's negative is read at all (> 1.0) - see IMAGE_CFG."""
    kind = (item or {}).get("kind")
    return CHAR_CFG if kind == "character" else (LOC_CFG if kind == "location" else IMAGE_CFG)


def sheet_prompt(item, auto=False):
    """The positive exactly as ComfyUI will get it (before a 重出意见 leads it).

    A person's own edit (`prompt_user`, the 提示词 editor) is used VERBATIM -
    no guard is added to text somebody wrote on purpose. Otherwise the stored
    prompt with the render-time guards every kind gets. auto=True ignores the
    override (what 恢复自动 would go back to)."""
    item = item or {}
    if not auto and str(item.get("prompt_user") or "").strip():
        return str(item["prompt_user"]).strip()
    text = str(item.get("prompt") or "")
    if item.get("kind") == "location":
        # stored prompts open with 空景无人: a denial that names 人
        text = text.replace("空景无人", "空景，只有景物与天光（empty scene, landscape and architecture only）", 1)
    if item.get("kind") == "character" and "纯白色背景板" not in text:
        # Every sheet is an ID photo on a white board (2026-09-25), stored
        # prompts from older projects included: said where the head sets
        # the background, or first if this head predates that.
        if "纯白色无缝背景" in text:
            text = text.replace("纯白色无缝背景", "背景只有一块纯白色背景板，纯白色无缝背景", 1)
        else:
            text = "证件照式人物参考图，背景只有一块纯白色背景板。" + text
    if item.get("kind") == "prop" and (str(item.get("id") or "").startswith("pw_") or item.get("weapon")):
        # the Chinese form of the weapon, stored prompts included (katana, 2026-09-28)
        text = filmwords.blade_look(text)
    if item.get("kind") == "character" and str(item.get("key", "")).endswith("_back") \
            and "人物背对镜头，看不到脸" not in text:
        # a back view is told again, at the end, that there is no face
        text = text.rstrip() + " 人物背对镜头，看不到脸。"
    return text


def sheet_negative(item, auto=False):
    """The negative exactly as ComfyUI will get it; `negative_user` verbatim."""
    item = item or {}
    if not auto and item.get("negative_user") is not None:
        return str(item["negative_user"]).strip()
    neg = str(item.get("negative") or "").strip()
    if item.get("kind") == "character":
        # A sheet is a person on white, nothing else. Its negative IS read
        # (CHAR_CFG), so the whole family of backgrounds is subtracted here -
        # for every sheet, stored prompts included.
        neg = ", ".join(x for x in (neg, CHAR_BG_NEGATIVE) if x)
    if item.get("kind") == "location":
        # nobody in a plate: people are subtracted, not denied in the positive
        neg = ", ".join(x for x in (LOC_PEOPLE_NEGATIVE, neg) if x)
    return neg


# HOT RELOAD (2026-09-30: "between each clip, we don't need restart service
# each time"). Every rule change used to mean restarting web_server, which
# throws away the shot in flight - up to two GPU-hours - and it happened many
# times a day. The rule modules are re-read at the start of every shot when
# their file is newer than the loaded copy, so a change to the word tables,
# the fight grade, the foley vocabulary, the look or the staging reaches the
# very next shot with nothing restarted. web_server's OWN code still needs a
# restart; put rules in these modules and it rarely will.
import importlib
_HOT = {}


def _hot_reload(log=None):
    for mod in (filmwords, fightcut, foley, look, stage, shotqa):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if mod.__name__ not in _HOT:
            _HOT[mod.__name__] = mt
        elif mt > _HOT[mod.__name__]:
            try:
                importlib.reload(mod)
                _HOT[mod.__name__] = mt
                if log:
                    log("   规则已更新，热加载 %s（不重启服务）" % mod.__name__)
            except Exception as exc:                        # noqa: BLE001
                if log:
                    log("！热加载 %s 失败，沿用旧版：%s" % (mod.__name__, str(exc)[:100]))


class Batch:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "assets", "batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.state = _read_json(self.path, None) or {"status": "idle", "queue": [], "done": [], "failed": []}

    # -- persistence ---------------------------------------------------
    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
        st["running"] = bool(self.thread and self.thread.is_alive())
        st["stop_requested"] = self.stop_flag
        return st

    # -- control -------------------------------------------------------
    def start(self, keys, mode, front=False):
        """keys: item keys to render, in order. mode: 'first' keeps each item's
        locked seed (a first render is reproducible); 'redo' draws a random one.
        front: put them ahead of what is already waiting (a single 重出 from the
        page should not sit behind sixty scene views)."""
        with self.lock:
            if self.thread and self.thread.is_alive():
                if front:
                    # a click from the page: already-waiting keys MOVE to the
                    # front (they used to stay put, 40 images back), and the one
                    # being drawn right now is drawn again after it - the person
                    # may have just written a note about exactly that picture.
                    self.state["queue"] = [k for k in self.state["queue"] if k not in set(keys)]
                    self.state["queue"][0:0] = list(keys)
                else:
                    # A 重出 goes to the BACK (2026-09-30: "增加重出，应该从后面
                    # 加入队列，不是前面"). A key already waiting stays where it
                    # is; the one being drawn right now IS queued again - that
                    # click is a request to redo exactly it.
                    have = set(self.state["queue"])
                    self.state["queue"].extend(k for k in keys if k not in have)
                self.state.setdefault("modes", {}).update({k: mode for k in keys})
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"])}
            self.state = {"status": "running", "queue": list(keys), "done": [], "failed": [],
                          "modes": {k: mode for k in keys}, "started": int(time.time()),
                          "current": None, "current_started": None, "last_error": "", "log": []}
            self._save()
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="batch-" + self.slug, daemon=True)
            self.thread.start()
            return {"ok": True, "queued": len(keys)}

    def resume(self):
        """Called at server start: a batch that was running when the server
        went down picks up where it left off."""
        st = self.state
        if st.get("status") == "running" and (st.get("queue") or st.get("current")):
            if st.get("current") and st["current"] not in st["queue"]:
                st["queue"].insert(0, st["current"])
            st["current"] = None
            # The job the old process submitted is still in ComfyUI and will
            # render for nobody, ahead of its own resubmission: 2026-09-24,
            # three restarts left char_ch06_back third in line behind two
            # orphans for 15 minutes. Take it out before starting again.
            old = st.pop("prompt_id", None)
            if old:
                try:
                    # POST /queue and /interrupt answer with an EMPTY body: parsed
                    # as JSON they raise, and the first version of this skipped
                    # the interrupt that way and left the orphan running.
                    urllib.request.urlopen(urllib.request.Request(
                        COMFY + "/queue", data=json.dumps({"delete": [old]}).encode(),
                        headers={"Content-Type": "application/json"}), timeout=10).read()
                    q = self._comfy("/queue", timeout=10)
                    if old in {x[1] for x in q.get("queue_running", [])}:
                        urllib.request.urlopen(urllib.request.Request(
                            COMFY + "/interrupt", data=b"{}",
                            headers={"Content-Type": "application/json"}), timeout=10).read()
                except Exception:                           # noqa: BLE001
                    pass
            self._log("服务重启，续跑：剩余 %d 张" % len(st["queue"]))
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="batch-" + self.slug, daemon=True)
            self.thread.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        self._log("收到停止：当前这张出完就停")

    def skip(self, keys, interrupt=True):
        """Drop waiting items (a picture accepted by hand does not need its re-render).
        interrupt=False touches only the queue: 取消排队 on a tile must never stop
        the picture being drawn, even if it started between click and request."""
        with self.lock:
            before = len(self.state["queue"])
            self.state["queue"] = [k for k in self.state["queue"] if k not in set(keys)]
            n = before - len(self.state["queue"])
            cur = self.state.get("current")
            self._save()
        if interrupt and cur in keys:
            # the one being drawn right now: interrupt ComfyUI; the render loop
            # sees the interrupted job, records it as failed, and moves on
            try:
                # /interrupt answers with an empty body, so no JSON parsing here
                urllib.request.urlopen(urllib.request.Request(COMFY + "/interrupt", data=b"{}",
                                       headers={"Content-Type": "application/json"}), timeout=10).read()
                n += 1
            except Exception:                               # noqa: BLE001
                pass
        if n:
            self._log("跳过 %d 张：%s" % (n, "、".join(keys)))
        return n

    def _log(self, msg):
        with self.lock:
            self.state.setdefault("log", []).append({"t": int(time.time()), "msg": msg})
            self.state["log"] = self.state["log"][-60:]
            self._save()

    # -- the loop --------------------------------------------------------
    def _run(self):
        while True:
            with self.lock:
                if self.stop_flag or not self.state["queue"]:
                    self.state["status"] = "stopped" if self.stop_flag else (
                        "done" if not self.state["failed"] else "done_with_failures")
                    self.state["current"] = None
                    self.state["finished"] = int(time.time())
                    self._save()
                    return
                key = self.state["queue"].pop(0)
                self.state["current"] = key
                self.state["current_started"] = int(time.time())
                self.state["step"] = 0
                self._save()
            try:
                item = self._item(key)
                if not item:
                    raise RuntimeError("prompts.json 里没有 %s" % key)
                mode = self.state.get("modes", {}).get(key, "first")
                bp = _read_json(os.path.join(PROJECTS, self.slug, "blueprint.json"), {}) or {}
                # The person's own note on this picture leads every render of it.
                note = str(item.get("user_note") or "").strip().rstrip("。")
                if item.get("kind") == "location" and note:
                    # a building's plates may talk about its courtyard; open ground's may not
                    lib_items = (_read_json(os.path.join(PROJECTS, self.slug, "assets", "prompts.json"), {}) or {}).get("items") or []
                    built = any(i.get("id") == item.get("id") and i.get("view") == "外·正面" for i in lib_items)
                    kept = filmwords.note_for_plate(note, built=built).rstrip("。")
                    if kept != note:
                        self._log("%s 的重出意见里提到人物或房屋，那几句不进提示词（人物已在负面提示词里）" % key)
                    note = kept
                if item.get("kind") == "character" and note:
                    kept = filmwords.note_for_sheet(note, back=str(key).endswith("_back")).rstrip("。")
                    if kept != note:
                        self._log("%s 的重出意见里提到随身物件，那几句不进提示词（说出名字就会画出来）" % key)
                    note = kept
                extra = ("【必须】" + note + "。") if note else ""
                full = os.path.join(PROJECTS, self.slug, "assets", "images", item["file"])
                for attempt in range(1 + (VERIFY_RETRIES if VERIFY_ENABLED else 0)):
                    if mode == "check" and attempt == 0:
                        # re-check the picture already on disk (its verdict was skipped
                        # or is stale); only render if it fails
                        n = _take_active(full)
                        if not n or not os.path.isfile(full):
                            raise RuntimeError("没有可核对的图")
                        # 人工认可 outranks the vision model: nothing re-checks it
                        tmeta = _read_json(os.path.join(_take_dir(full)[0], "%d.json" % n), {}) or {}
                        if (tmeta.get("check") or {}).get("manual"):
                            self._log("✓ %s 已人工认可，不再核对" % item["key"])
                            break
                        with open(full, "rb") as f:
                            png = f.read()
                    else:
                        n, png = self._render(item, "redo" if (attempt or mode in ("redo", "check")) else mode, extra)
                    if not VERIFY_ENABLED:
                        break
                    with self.lock:
                        self.state["checking"] = item["key"]
                        self._save()
                    verdict = _verify(item, png, bp)
                    with self.lock:
                        self.state["checking"] = None
                    verdict["attempt"] = attempt + 1
                    full = os.path.join(PROJECTS, self.slug, "assets", "images", item["file"])
                    _take_note(full, n, {"check": verdict})
                    if verdict["ok"] is None:
                        self._log("？ %s 审核模型不可用，跳过审核" % item["key"])
                        break
                    if verdict["ok"]:
                        self._log("✓ 审核通过 %s（第 %d 次）" % (item["key"], n))
                        break
                    self._log("⚠ %s 第 %d 次：%s" % (item["key"], n, "；".join(verdict["problems"])[:160]))
                    if attempt < VERIFY_RETRIES:
                        # The checker's words are ADVICE, shown on the tile - never a
                        # prompt. Its "fix" once ordered a sword onto 段誉, who carries
                        # none. A retry is a new seed with the person's note only.
                        extra = ("【必须】" + note + "。") if note else ""
                        self._log("↻ 按纠正重出 %s" % item["key"])
                    else:
                        self._log("× %s 重出 %d 次仍未通过，保留最后一张并标记" % (item["key"], VERIFY_RETRIES))
                with self.lock:
                    self.state["done"].append(key)
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["failed"].append({"key": key, "error": str(exc)[:300], "t": int(time.time())})
                    self.state["last_error"] = str(exc)[:300]
                self._log("× %s：%s" % (key, str(exc)[:160]))
            finally:
                with self.lock:
                    self.state["current"] = None
                    self.state["checking"] = None
                    self._save()

    def _item(self, key):
        lib = _read_json(os.path.join(PROJECTS, self.slug, "assets", "prompts.json"), {}) or {}
        return next((i for i in lib.get("items", []) if i.get("key") == key), None)

    # -- one image -----------------------------------------------------
    def _comfy(self, path, body=None, timeout=60):
        req = urllib.request.Request(COMFY + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)

    def _render(self, item, mode, extra=""):
        wf = _read_json(WORKFLOW, None)
        if not wf:
            raise RuntimeError("读不到工作流 %s" % WORKFLOW)
        wf.pop("_comment", None)
        # A correction appended to a 700-character prompt is the least-weighted thing in it;
        # 陆横 kept his drawn sword through two of them. It goes in front, where the model listens.
        # What the person sees in the 提示词 editor is exactly this: the
        # composed text, or their own override verbatim (sheet_prompt / _negative).
        text = sheet_prompt(item)
        wf["20"]["inputs"]["text"] = ((extra + " ") if extra else "") + text
        # The negative only exists in the graph when there is a guider that can
        # subtract it; see IMAGE_CFG. Built here rather than in the template so
        # the template stays the thing the console posts.
        neg = sheet_negative(item)
        cfg = sheet_cfg(item)
        if cfg > 1.0 and neg:
            wf["21"] = {"class_type": "CLIPTextEncode",
                        "inputs": {"text": neg, "clip": wf["20"]["inputs"]["clip"]}}
            wf["41"] = {"class_type": "CFGGuider",
                        "inputs": {"model": wf["41"]["inputs"]["model"],
                                   "positive": wf["41"]["inputs"]["conditioning"],
                                   "negative": ["21", 0], "cfg": cfg}}
        full = os.path.join(PROJECTS, self.slug, "assets", "images", item["file"])
        seed = item.get("seed") or 0
        if mode == "redo" or _take_list(full):
            seed = random.getrandbits(32)
        sn = wf["40"]["inputs"]
        if "noise_seed" in sn:
            sn["noise_seed"] = seed
        elif "seed" in sn:
            sn["seed"] = seed
        wide = (item.get("aspect") or "9:16") == "16:9"
        for n in ("30", "44"):
            if "width" in wf.get(n, {}).get("inputs", {}):
                wf[n]["inputs"]["width"], wf[n]["inputs"]["height"] = (1920, 1080) if wide else (1080, 1920)
        if "__PROMPT__" in json.dumps(wf, ensure_ascii=False):
            raise RuntimeError("工作流里残留 __PROMPT__ 占位符")

        # Submit, retrying for as long as it takes: ComfyUI restarting is not a
        # reason to abandon a batch, and nothing else is going to resubmit.
        pid = None
        while pid is None:
            if self.stop_flag:
                raise RuntimeError("停止")
            try:
                pid = self._comfy("/prompt", {"prompt": wf, "client_id": BATCH_CLIENT})["prompt_id"]
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["last_error"] = "ComfyUI 不可达，30 秒后重试：%s" % str(exc)[:120]
                    self._save()
                time.sleep(30)
        with self.lock:
            self.state["prompt_id"] = pid
            self.state["seed"] = seed
            self.state["last_error"] = ""
            self._save()

        t0 = time.time()
        while True:
            time.sleep(3)
            try:
                rec = self._comfy("/history/" + pid, timeout=30).get(pid)
            except Exception:                               # noqa: BLE001
                # ComfyUI went away mid-render. If it comes back the job is
                # gone with it; resubmit rather than wait forever.
                if time.time() - t0 > 120:
                    try:
                        self._comfy("/queue", timeout=10)
                        rec = self._comfy("/history/" + pid, timeout=30).get(pid)
                        if rec is None:
                            q = self._comfy("/queue", timeout=10)
                            ids = {x[1] for x in q.get("queue_running", []) + q.get("queue_pending", [])}
                            if pid not in ids:
                                self._log("ComfyUI 重启过，重新提交 %s" % item["key"])
                                return self._render(item, mode, extra)
                    except Exception:                       # noqa: BLE001
                        pass
                continue
            if not rec:
                if time.time() - t0 > 3 * 3600:
                    raise RuntimeError("等了 3 小时没有结果")
                # A ComfyUI that restarted between two polls never raised
                # above - it answered, with an empty history - and the job
                # died with the old process. 2026-09-24: an image sat 48 min
                # "rendering" against an idle, empty ComfyUI. Absent from
                # history AND queue twice, 10 s apart, means gone.
                if int(time.time() - t0) % 60 < 3 and time.time() - t0 > 60:
                    try:
                        gone = 0
                        for _ in range(2):
                            q = self._comfy("/queue", timeout=10)
                            ids = {x[1] for x in q.get("queue_running", []) + q.get("queue_pending", [])}
                            if pid in ids or self._comfy("/history/" + pid, timeout=30).get(pid):
                                break
                            gone += 1
                            time.sleep(10)
                        if gone == 2:
                            self._log("ComfyUI 已不认得 %s 的任务，重新提交" % item["key"])
                            return self._render(item, mode, extra)
                    except Exception:                       # noqa: BLE001
                        pass
                continue
            st = rec.get("status", {})
            if st.get("status_str") == "error":
                raise RuntimeError("ComfyUI 任务失败：%s" % json.dumps(st.get("messages", [])[-1:], ensure_ascii=False)[:200])
            for out in rec.get("outputs", {}).values():
                for im in out.get("images", []):
                    url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                        COMFY, urllib.parse.quote(im["filename"]), urllib.parse.quote(im.get("subfolder", "")), im.get("type", "output"))
                    with urllib.request.urlopen(url, timeout=120) as r:
                        body = r.read()
                    if not body:
                        raise RuntimeError("取到 0 字节")
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    n = _take_add(full, body, {"seed": seed, "prompt_id": pid, "elapsed": int(time.time() - t0),
                                               "extra": extra or ""})
                    self._log("✓ %s 第 %d 次，%d 秒" % (item["key"], n, int(time.time() - t0)))
                    return n, body
            if st.get("completed") and not rec.get("outputs"):
                raise RuntimeError("完成但没有输出")


# ----------------------------------------------------------------------
# step 3 -- the video queue
# ----------------------------------------------------------------------
# One clip per shot, from the script and the image library. The script's own
# first_frame_ref names a character for 84 of xuemang's 108 shots, and a
# character asset is a passport plate on a plain ground - starting a shot
# there would open every one of them on a white studio背景. So the first
# frame is chosen from the LOCATION plates instead (matching the shot's
# location, its time of day and whether the slug line says 内景 or 外景);
# who is in the shot is carried by the video_prompt text.
H3 = "http://127.0.0.1:9000"
# A shot's target length, assembled from SEG_SECONDS segments rather than asked
# of h3 in one call. 15 s is what the film needs: the script writes 10 s shots
# and the narration for them synthesizes to 10-13 s, so anything shorter cannot
# carry its own line. Measured 2026-09-08 on the same shot,
# same first frame, one clip each:
#     56 frames 2.33s 1312x736  13:00 total,  8:06 sampling  -> 5.6 GPU-min/film-s
#     73 frames 3.04s 1312x736  17:52 total, 11:33 sampling  -> 5.9
#    107 frames 4.46s 1088x608  16:35 total, 10:44 sampling  -> 3.7
# Cost is frames x pixels and roughly linear in frames (1.38x for 1.30x), so
# 15 s costs 15 s of GPU however it is sliced: about 3.7 GPU-minutes per second
# of finished film at 1088x608 (~56 min a shot) and ~5.9 at 1312x736 (~88 min),
# which is what it runs at now. Canvas comes from H3_MAX_PIXELS in launch.sh,
# not from this constant.
# A CAP, not a floor. It used to be a floor - target was max(MIN, 15, voice) -
# so a shot carrying a four-character line still got fifteen seconds of
# picture. Measured over the 51 takes on disk: 8.8 s of dead air per voiced
# clip on average (C01S02 said 1.3 s over 15.0 s), and 24 of 51 clips had no
# audio at all and ran fifteen silent seconds. That is what made the chapter
# feel wrong when the shots were cut together, and it is also why a chapter
# costs what it costs: sizing the picture to what it carries is ~38% of the
# render time.
# What sizes the picture is the line, and nothing else may cut it short. There
# used to be a second cap here as well - BOOKREEL_CLIP_SECONDS, 15 s - and a
# cap on the PICTURE is a cut in the SOUND, because the mux is -shortest: a
# shot whose narration ran past its clip had the line chopped off mid-word,
# and its cues went on describing audio that is not in the file. Measured over
# the 41 voiced takes on disk (2026-09-13): 4 were cut, C01S04 losing 12.52 s
# of a 27.52 s line and C01S25 10.84 s, and their subtitles ran up to 11 s
# past the last thing heard - which in a concatenated chapter lands them on
# top of the next shot's. That constant is gone rather than raised, so nobody
# sets it back to 15 and quietly starts cutting sentences again.
#
# This is the one ceiling left, and it is a guard against a runaway line, not
# a length policy: at ~5.9 GPU-min per second of film it is ~3 h for a single
# shot. A shot that actually reaches it IS having its sentence cut, so it is
# logged by name with the seconds it lost.
VIDEO_VOICE_MAX_SECONDS = float(os.environ.get("BOOKREEL_CLIP_VOICE_SECONDS", "30.0"))
# How much picture to keep after the last word - room to land the line and to
# cut on, not another beat of story.
VIDEO_HANDLE_SECONDS = float(os.environ.get("BOOKREEL_CLIP_HANDLE", "1.5"))
# A shot with nothing said still has to breathe, but not equally: a wide
# establishing the courtyard holds, a cutaway to a hand on a seal does not.
# Sizing the 24 silent shots off shot_size keeps them from all coming out the
# same length, which is half of what made the cut feel mechanical.
VIDEO_SILENT_SECONDS = float(os.environ.get("BOOKREEL_CLIP_SILENT_SECONDS", "3.75"))
VIDEO_SILENT_WIDE_SECONDS = float(os.environ.get("BOOKREEL_CLIP_SILENT_WIDE_SECONDS", "7.5"))
# A floor as well as a cap. Every shot in this project asks for 10 s, so the cap
# is what binds and the floor never fires -- but a script with a 1.5 s beat in
# it would otherwise render 39 frames, and a clip under two seconds is not a
# shot, it is a flicker. Nothing may go out shorter than this.
VIDEO_MIN_SECONDS = float(os.environ.get("BOOKREEL_CLIP_MIN_SECONDS", "2.5"))
# 16:9 landscape - the finished film is widescreen, and the scene plates the
# clips start from are 16:9 too. (Character sheets stay 9:16; they are
# reference art, not frames.)
VIDEO_RESOLUTION = os.environ.get("BOOKREEL_CLIP_RESOLUTION", "1920x1080")
VIDEO_FORMATS = {"16:9": "1920x1080", "9:16": "1080x1920"}

_TOD = [("夜", ("夜", "晚")), ("晨", ("晨", "黎明", "清晨")), ("日", ("日", "昼", "午", "白天"))]


def _shot_tod(shot):
    text = "%s %s" % (shot.get("slug_line") or "", shot.get("time_of_day") or "")
    for tod, words in _TOD:
        if any(w in text for w in words):
            return tod
    return "日"


# ----------------------------------------------------------------------
# the first frame of a COLD shot -- the place, with the subject already in it
# ----------------------------------------------------------------------
# h3 PINS the first frame, so whatever is in it is in the clip: a plate with a
# man in the alley put that man in C01S01, and a plate with nobody in it made
# h3 invent the subject - twice, side by side, both writing, in C01S02. Both
# are the same defect from opposite ends: the model is being asked to decide
# who is in the picture, and it is the one party in this pipeline with no idea
# how many people the scene has.
#
# So a cold shot gets its own first frame: the SAME plate view it would have
# opened on (that view's text is written, verified and consistent with the
# other shots there), with the subject painted into it and the count stated
# the way the reference sheets state prop counts. Flux has never painted a
# counted sentence and has no working negative, so this is said positively,
# and the picture is then CHECKED - exactly one person - before any GPU goes
# into sampling video from it. One image is ~6.5 min against the ~2 GPU-hours
# a re-rendered clip costs.
FIRSTFRAME = os.environ.get("BOOKREEL_FIRST_FRAME", "1") not in ("0", "", "false")
FIRSTFRAME_TRIES = int(os.environ.get("BOOKREEL_FIRST_FRAME_TRIES", "2"))
# What the frame should look like, by shot size. Said as what a viewer sees -
# the same rule the video prompts follow - because an apparatus noun is not a
# thing a picture can contain.
_FF_FRAMING = {
    "ELS": "人在画面里很小，四周的环境占满画面",
    "LS": "人物全身在画面里，环境占大部分画面",
    "MLS": "人物从膝盖往上在画面里",
    "MS": "人物从腰往上在画面里",
    "MCU": "人物从胸口往上在画面里",
    "CU": "人物的头和肩在画面里",
    "BCU": "人物的脸占满画面",
    "ECU": "只有五官的一小部分占满画面",
}


# (no 机位/镜头 in them: the jargon strip that runs over the head cuts those)
_FF_ANGLE = {"俯角": "从高处往下俯拍", "仰角": "从低处往上仰拍，人物显得高大有压迫感",
             "鸟瞰": "从正上方往下看", "低角贴地": "贴着地面往上仰拍，脚步和地面在前景",
             "斜角": "画面倾斜", "过肩": "越过前景一人的肩膀看向对手"}
FF_COMBAT = ("动作定格在出手的一瞬：身体完全舒展，重心压低，弓步前冲或腾身而起，"
             "衣摆和发梢被动作猛地甩起，兵刃挥过处带着运动模糊的拖影，脚下扬起细尘，"
             "动感强烈，像武侠电影的剧照")


def first_frame_prompt(shot, bp, plate):
    """The plate's own scene text, with this shot's subject standing in it."""
    ids = cast_ids(shot, bp)
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    who, crowd = [], []
    # Only the DUELLISTS have a blade out. Every armed character in a fight
    # shot was told 手里握着一把出鞘的长剑, so the seated sect master watching
    # from his chair was drawn sword in hand too. Seated/kneeling in the
    # staging's first beat means watching; at most two draw.
    st0 = (((shot.get("staging") or {}).get("beats") or [{}])[0]
           if isinstance(shot.get("staging"), dict) else {}) or {}
    standing = [i for i in ids if str((st0.get(i) or {}).get("posture") or "stand") not in ("sit", "kneel", "lie")]
    # the fighters by name first (C01S02: 段誉 was standing and so drew too)
    fw = fighters(shot, bp)
    drawn = set(fw or (standing or ids)[:2])
    for i in ids:
        c = by_id[i]
        cos = re.split(r"[，。;；,]", _plain(c.get("costume")) or "")[0].strip()
        if is_group(c):
            crowd.append("周围还有一群%s%s" % (_plain(c.get("name")).rstrip("们"), ("，穿" + cos) if cos else ""))
            continue
        g = _plain(c.get("gender"))
        sex = "男子" if g.startswith("男") else ("女子" if g.startswith("女") else "人")
        bit = _age_words(c.get("age")) + sex
        if cos:
            bit += "，穿%s" % filmwords.strip_weapons(cos)
        wp = weapon_prop(bp, i)
        if wp:
            noun = re.split(r"[（(]", wp.get("name") or "")[0] or "兵器"
            # In a fight the blade is in the hand. Told 腰间挂着一把带鞘的长剑
            # while the scene says 持剑对拆, Flux drew both: tianlongbabu C01S02
            # opened on two men each with a drawn sword AND a sheathed one.
            # One blade, and the sheath it came out of is empty.
            # …and it is a CHINESE blade: without its form spelled out the
            # sheet's katana grip and disc guard came through (2026-09-28).
            look_ = filmwords.blade_short(filmwords.blade_noun(noun))
            look_ = "（%s）" % look_ if look_ else ""
            if is_combat(shot) and i in drawn:
                bit += "，手里握着一把出鞘的%s%s，腰间挂着空鞘" % (noun, look_)
            elif not is_combat(shot):
                bit += "，腰间挂着一把带鞘的%s%s" % (noun, look_)
        if is_combat(shot) and i not in drawn:
            bit += "，在一旁双手空着观看"
        who.append(bit)
    if not who and not crowd:
        return ""
    n = len(who)
    if crowd:
        # no count to state: the named people are listed and the crowd is a crowd
        cnt = "画面中有很多人"
        who = who + crowd
    else:
        cnt = {1: "画面中只有一个人", 2: "画面中只有两个人"}.get(n, "画面中只有%d个人" % n)
    _fw = fighters(shot, bp)
    if _fw and len(cast_ids(shot, bp)) > len(_fw):
        # Flux has never painted a count; say who fights and who watches
        cnt += "，场中央只有%s在交手，其余的人都在一旁观看" % (
            {1: "一个人", 2: "两个人", 3: "三个人"}.get(len(_fw), "%d个人" % len(_fw)))
    people = _plain((bp.get("world") or {}).get("people")) or ""
    cam = (shot.get("camera") or {}) if isinstance(shot.get("camera"), dict) else {}
    size = str(cam.get("shot_size") or "MS").strip().upper()
    framing = _FF_FRAMING.get(size, _FF_FRAMING["MS"])
    # The ANGLE reaches the first frame too. It never did, so every cold shot
    # opened at eye level whatever the 分镜 asked, and h3 keeps frame 0's
    # camera: the 仰角 on the attacker and the 低角贴地 on the footwork that
    # the fight rules ask for were written and never seen. Flux does not paint
    # captions, so here the apparatus words are allowed.
    ang = _FF_ANGLE.get(_plain(cam.get("angle")), "")
    if ang:
        framing += "，" + ang
    if is_combat(shot):
        # A fight's frame 0 is the middle of a move, not two men posing: h3
        # starts from it, and a still that already has speed in it is the
        # difference between a strike and a rehearsal.
        framing += "，" + FF_COMBAT
    # What the person is DOING, so the pinned frame is the shot's own moment
    # rather than a man standing in a courtyard. First clause only: the rest of
    # `action` is simultaneous description (CLAUDE.md) and reads as a list.
    act = _denames(re.split(r"[，。;；]", filmwords.soften_face(filmwords.strip_camera(_plain(shot.get("action")) or ""),
                                                              combat=is_combat(shot)))[0].strip(), bp)
    # THE SCENE COMES FROM THE SHOT, NOT FROM THE PLATE. The plate view is
    # picked for variety and knows nothing about where the action is: C01S02
    # plays at the stone table in the yard and its plate is the building's
    # right-hand side wall, so composing the two gives a man sitting on a
    # stool that is not in the picture. The 分镜's own scene prose is the one
    # description of this shot's frame, and it is the text the clip is
    # rendered from - so the first frame and the clip describe one picture.
    # The plate still contributes what is true of the PLACE whatever the
    # camera does: its light and its material style.
    # Both halves get the whole word table AND the notation strip. The light
    # line went in raw until 2026-09-23, so C01S26's first frame was told
    # 月光 and 碎斑 (a moon disc and a spotted floor, the plate rules this file
    # already has) and 85mm / Kodak Vision3 / 16:9, and it is pinned as frame
    # 0 of the clip: whatever it draws, h3 keeps.
    # Softened like the clip's own prompt: the scene prose went in raw and
    # C01S06's first frame was told 眉头拧起、嘴唇抿紧、下颌一绷 - the grimace
    # h3 then held for the whole clip (2026-10-14: "段誉表情不自然").
    scene = filmwords.dedigit(filmwords.substitute(filmwords.soften_face(filmwords.strip_camera(filmwords.strip_glow(
        _plain(shot.get("video_prompt_raw")) or "")), combat=is_combat(shot))))
    if is_combat(shot):
        scene = filmwords.sword_paths(scene)
    else:
        # …and what happens NEXT in the prose (哼出一口气后随即眉头微皱) is not
        # in frame 0 either: the clip does that part
        scene = re.sub(r"[，、]?后?(?:随即|然后|接着|紧接着|继而|旋即)[^，。；]*", "", scene)
    parts = plate.get("parts") or {}
    place = "，".join(filmwords.dedigit(filmwords.substitute(x))
                     for x in (_plain(parts.get("light")), _plain(parts.get("style"))) if x)
    # The FACE of this moment. The first frame is conditioned on the front
    # sheets, which are 中性表情 by design, and h3 keeps frame 0 - so a face
    # nobody told to do anything stayed neutral for the whole clip.
    face = _denames(_performance_clause(shot, bp, face_only=True), bp)
    # A fight's frame 0 is pinned, and a frame of two men in guard stance
    # starts a clip of two men in guard stance. Open mid-move.
    if is_combat(shot):
        act = (act + "，" if act else "") + "两人都在大幅度的动作之中，身体前冲或腾空，衣摆飞起"
        # Frame 0 sets where the duel stands, and h3 keeps it: two profiles
        # side by side across the frame start a clip on a rail. One nearer,
        # one deeper in, on a diagonal (2026-09-30).
    # Said mid-prompt it was ignored: the first frame of 2026-09-30 put the
    # two men side by side in profile on one line anyway. Opening tokens set
    # the picture, so the duel's depth goes straight after the count, as a
    # concrete composition - one back near us, the other face-on deeper in.
    depth = ""
    # …but not with named onlookers: the onlookers stand
    # right behind the pair there, and the near/far staging turned one of
    # them into a third duellist - C01S02 drew 左子穆 sword in hand between
    # the two fighters on three tries out of three, and the C01S01 wide did
    # the same the next hour (2026-10-04). Any named onlooker drops it.
    _watchers = [i for i in ids if i not in (fw or []) and not is_group(by_id.get(i))]
    if is_combat(shot) and len(who) >= 2 and not _watchers:
        # The near and far figures ARE the two duellists, said by their
        # clothes. Said as "近处一人 / 另一人" next to scene prose that
        # describes the pair again, Flux drew three: a near back plus both
        # fighters (tianlongbabu C01S02, twice on 2026-10-04).
        def _cos(i):
            c = by_id.get(i) or {}
            return re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                            filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
        pair = [i for i in (fw or ids) if not is_group(by_id.get(i))][:2]
        a_, b_ = (_cos(pair[0]), _cos(pair[1])) if len(pair) == 2 else ("", "")
        na = ("穿%s的那人" % a_) if a_ else "一人"
        nb = ("穿%s的那人" % b_) if b_ else "另一人"
        depth = ("场中交手的只有这两个人，一前一后、一近一远：近处是%s，在画面左下方，背对着看画面的人，"
                 "只看得见他的后背、后脑和甩起的衣摆，正挥剑向前扑去；远处是%s，在他前方的"
                 "场地深处、画面中间偏右，正面朝着我们迎着冲来，身形比近处那人小得多。" % (na, nb))
    # Who talks to whom, turned to each other - frame 0 is where h3 takes
    # its composition from, and a first frame of two men square to the lens
    # starts a clip of two men square to the lens (C01S14, 2026-10-09).
    facing = _denames(_facing_clause(shot, bp), bp)
    # …and the hall behind them stays full (`_crowd_bg_clause`).
    crowd_bg = _denames(_crowd_bg_clause(shot, bp), bp)
    head = "%s，%s%s。%s%s。%s%s%s%s" % (cnt, (people + "，") if people else "", "、".join(who), depth,
                                       (act or "站在这里"), face, facing, crowd_bg, framing)
    # 25岁 -> 二十五岁, as the video prompt already writes it: a bare digit is
    # the easiest thing there is to paint.
    head = filmwords.dedigit(head)
    # The location's style line ends "画幅9:16竖构图" - it was written for a
    # plate - and a 9:16 instruction inside a 16:9 render is a contradiction
    # the model resolves by letterboxing. Same fix the video prompts use.
    return _fix_aspect("%s。%s%s %s" % (head, scene, ("。" + place) if place else "",
                                       look.always_positive("image")),
                       plate.get("aspect") or "16:9")


# THE SHEETS NEVER REACHED A PICTURE. Every reference sheet and plate was
# rendered, verified, curated - and then the first frame of a shot was drawn
# from TEXT, the Flux graph having no image input at all. A face was
# "consistent" across shots exactly as far as two readings of one description
# agree. Flux.2 takes reference images natively (ReferenceLatent ->
# ref_latents, chainable), so a first frame is now conditioned on the front
# sheet of each named character in the shot and on the location plate. The
# sheets are keyed by character id for the whole project, which is what keeps
# a person the same person from chapter 1 to the last.
_PROMPTS_LOCK = threading.Lock()
FIRSTFRAME_REFS = os.environ.get("BOOKREEL_FIRST_FRAME_REFS", "1") not in ("0", "", "false")
# 0.6 x seven references put a crowd fight's first frame at 308 s/step, 45
# min a frame (2026-09-30) - every reference is tokens the sampler attends to.
REF_MEGAPIXELS = float(os.environ.get("BOOKREEL_REF_MEGAPIXELS", "0.45"))
FIRSTFRAME_MAX_REFS = int(os.environ.get("BOOKREEL_FIRST_FRAME_MAX_REFS", "4"))


def _comfy_upload(path):
    """Put a local image into ComfyUI's input folder; returns the name LoadImage wants."""
    boundary = "----bookreel%08x" % random.getrandbits(32)
    name = "bookreel_%s_%s" % (hashlib.sha1(os.path.abspath(path).encode()).hexdigest()[:10],
                               os.path.basename(path))
    with open(path, "rb") as f:
        data = f.read()
    body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
             "Content-Type: image/png\r\n\r\n" % (boundary, name)).encode() + data +
            ("\r\n--%s\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue"
             "\r\n--%s--\r\n" % (boundary, boundary)).encode())
    req = urllib.request.Request(COMFY + "/upload/image", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["name"]


def _add_refs(wf, refs):
    """Chain each reference image onto the positive conditioning, before FluxGuidance."""
    cond = ["20", 0]
    for i, path in enumerate(refs or []):
        b = 70 + 4 * i
        wf[str(b)] = {"class_type": "LoadImage", "inputs": {"image": _comfy_upload(path)}}
        wf[str(b + 1)] = {"class_type": "ImageScaleToTotalPixels",
                          "inputs": {"image": [str(b), 0], "upscale_method": "lanczos",
                                     "megapixels": REF_MEGAPIXELS, "resolution_steps": 1}}
        wf[str(b + 2)] = {"class_type": "VAEEncode",
                          "inputs": {"pixels": [str(b + 1), 0], "vae": ["12", 0]}}
        wf[str(b + 3)] = {"class_type": "ReferenceLatent",
                          "inputs": {"conditioning": cond, "latent": [str(b + 2), 0]}}
        cond = [str(b + 3), 0]
    wf["22"]["inputs"]["conditioning"] = cond


def _render_image_bytes(prompt, negative, aspect="16:9", seed=None, stop=None, timeout=3600,
                        refs=None):
    """One Flux render -> PNG bytes. The image QUEUE keeps its own copy of this
    dance (Batch._render) because it also writes takes, logs and batch state;
    this is the same graph with none of that, for a caller that is not the
    queue."""
    wf = _read_json(WORKFLOW, None)
    if not wf:
        raise RuntimeError("读不到工作流 %s" % WORKFLOW)
    wf.pop("_comment", None)
    wf["20"]["inputs"]["text"] = prompt
    if refs:
        _add_refs(wf, refs)
    if IMAGE_CFG > 1.0 and negative:
        wf["21"] = {"class_type": "CLIPTextEncode",
                    "inputs": {"text": negative, "clip": wf["20"]["inputs"]["clip"]}}
        wf["41"] = {"class_type": "CFGGuider",
                    "inputs": {"model": wf["41"]["inputs"]["model"],
                               "positive": wf["41"]["inputs"]["conditioning"],
                               "negative": ["21", 0], "cfg": IMAGE_CFG}}
    sn = wf["40"]["inputs"]
    seed = int(seed if seed is not None else random.getrandbits(32))
    if "noise_seed" in sn:
        sn["noise_seed"] = seed
    elif "seed" in sn:
        sn["seed"] = seed
    wide = aspect == "16:9"
    for k in ("30", "44"):
        if "width" in wf.get(k, {}).get("inputs", {}):
            wf[k]["inputs"]["width"], wf[k]["inputs"]["height"] = (1920, 1080) if wide else (1080, 1920)
    req = urllib.request.Request(COMFY + "/prompt",
                                 data=json.dumps({"prompt": wf, "client_id": BATCH_CLIENT}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        pid = json.load(r)["prompt_id"]
    t0 = time.time()
    while True:
        time.sleep(3)
        if stop and stop():
            raise RuntimeError("停止")
        if time.time() - t0 > timeout:
            # Take it out of ComfyUI before giving up: an abandoned job kept
            # sampling for nobody and the fallback queued behind it
            # (C01S01, 2026-09-30).
            _comfy_drop(pid)
            raise RuntimeError("首帧等了 %d 分钟没有结果" % (timeout // 60))
        try:
            with urllib.request.urlopen(COMFY + "/history/" + pid, timeout=30) as r:
                rec = json.load(r).get(pid)
        except Exception:                                   # noqa: BLE001
            continue
        if not rec:
            continue
        if rec.get("status", {}).get("status_str") == "error":
            err = next((m[1] for m in rec["status"].get("messages", []) if m[0] == "execution_error"), {})
            raise RuntimeError("ComfyUI 任务失败：%s %s" % (err.get("node_type", ""), str(err.get("exception_message", ""))[:200]))
        for out in rec.get("outputs", {}).values():
            for im in (out.get("images") or []):
                url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                    COMFY, urllib.parse.quote(im["filename"]),
                    urllib.parse.quote(im.get("subfolder", "")), im.get("type", "output"))
                with urllib.request.urlopen(url, timeout=120) as r:
                    body = r.read()
                if body:
                    return body, seed, int(time.time() - t0)
        if rec.get("outputs"):
            raise RuntimeError("完成但没有输出")


def first_frame_path(slug, shot):
    return os.path.join(PROJECTS, slug, "assets", "firstframes", "%s.png" % shot.get("id"))


def ensure_first_frame(slug, shot, bp, plate, want, stop=None, log=None, note="", cont=""):
    """A verified first frame for a cold shot, or "" to fall back to the plate.

    Idempotent like the 4K pass: a file at least as new as the script it came
    from is that script's. Never fatal - a shot with no first frame renders
    from the plate exactly as it did before, which is the fall-through every
    un-migrated chapter already takes.
    """
    out = first_frame_path(slug, shot)
    prompt = first_frame_prompt(shot, bp, plate)
    if prompt and note:
        # the person's 重出意见 leads, as on every other image
        prompt = "【必须】" + note.strip().rstrip("。") + "。" + prompt
    refs = first_frame_refs(slug, shot, bp, plate, cont=cont) if FIRSTFRAME_REFS else []
    # the folder too: _faces/char_ch06.png (a bust crop) is not char_ch06.png
    sig = ["%s/%s@%d" % (os.path.basename(os.path.dirname(r)), os.path.basename(r),
                         int(os.path.getmtime(r))) for r in refs]
    # Stale means "built from different text", not merely "older". This used
    # to return any file that existed, so a rule fixed in filmwords never
    # reached a first frame already on disk: C01S26's, drawn 2026-09-21 with
    # brush writing down the subject's chest, was re-used by every re-render
    # after the rule that caused it was gone - and h3 pins frame 0.
    if os.path.isfile(out):
        try:
            with open(out + ".json", encoding="utf-8") as fh:
                meta = json.load(fh)
            old, old_sig = meta.get("prompt"), meta.get("refs") or []
        except (OSError, ValueError):
            old, old_sig = None, []
        # A new sheet (a re-render, a curated take) is a new face: same rule.
        if not prompt or (old == prompt and old_sig == sig):
            return out
        if log:
            log("   %s 首帧文字已变，重画" % shot.get("id"))
    if not prompt:
        return ""
    neg = str(plate.get("negative") or "")
    rejected = []
    for attempt in range(FIRSTFRAME_TRIES):
        try:
            body, seed, secs = _render_image_bytes(prompt, neg, plate.get("aspect") or "16:9", stop=stop,
                                                   refs=refs, timeout=FIRSTFRAME_TIMEOUT)
        except RuntimeError as exc:
            if not refs or "停止" in str(exc):
                raise
            # a reference that will not load must not cost the shot its first frame
            if log:
                log("   %s 首帧带参考图失败，改为只按文字出：%s" % (shot.get("id"), str(exc)[:120]))
            refs, sig = [], []
            body, seed, secs = _render_image_bytes(prompt, neg, plate.get("aspect") or "16:9", stop=stop)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".part.png"
        with open(tmp, "wb") as f:
            f.write(body)
        ok, n = image_headcount(tmp, want)
        if ok is False:
            if log:
                log("   %s 首帧第 %d 次画了 %d 个人，存档后重画" % (shot.get("id"), attempt + 1, n))
            rejected.append(_keep_rejected(tmp, shot, attempt, "画了 %d 个人，应为 %s" % (n, want),
                                           prompt, seed, refs))
            continue
        fw = len(fighters(shot, bp))
        if fw:
            okf, nf = fighter_check(tmp, fw)
            if okf is False:
                if log:
                    log("   %s 首帧第 %d 次有 %d 个人在打，应为 %d 个，存档后重画"
                        % (shot.get("id"), attempt + 1, nf, fw))
                rejected.append(_keep_rejected(tmp, shot, attempt, "有 %d 个人在打，应为 %d 个" % (nf, fw),
                                               prompt, seed, refs))
                continue
        os.replace(tmp, out)
        with open(out + ".json", "w", encoding="utf-8") as fh:
            json.dump({"prompt": prompt, "seed": seed, "seconds": secs,
                       "plate": plate.get("key"), "heads": n, "refs": sig}, fh, ensure_ascii=False)
        if log:
            log("   %s 首帧已生成（%s，%d 秒，人数 %s）"
                % (shot.get("id"), plate.get("key"), secs, n))
        return out
    # Every try was flagged. The checker does not decide (2026-10-01): the
    # last flagged frame is used, not the empty plate, and every one of them
    # stays in firstframes/_rejected for the person to look at.
    if rejected:
        src, why = rejected[-1]
        shutil.copyfile(src, out)
        with open(out + ".json", "w", encoding="utf-8") as fh:
            json.dump({"prompt": prompt, "plate": plate.get("key"), "refs": sig,
                       "flagged": why, "from": os.path.basename(src)}, fh, ensure_ascii=False)
        if log:
            log("⚠ %s 首帧系统认为有问题（%s），照用，由你决定；各版本在 firstframes/_rejected/"
                % (shot.get("id"), why))
        return out
    return ""


def _keep_rejected(tmp, shot, attempt, why, prompt, seed, refs):
    """Move a flagged first frame aside instead of deleting it."""
    d = os.path.join(os.path.dirname(tmp), "_rejected")
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "%s-%s-%d.png" % (shot.get("id"), time.strftime("%m%d%H%M%S"), attempt + 1))
    os.replace(tmp, dst)
    with open(dst + ".json", "w", encoding="utf-8") as fh:
        json.dump({"why": why, "prompt": prompt, "seed": seed,
                   "refs": [os.path.basename(r) for r in refs or []]}, fh, ensure_ascii=False)
    return dst, why


def weapon_prop(bp, cid):
    """The accessory prop for a character's weapon (sheets carry none)."""
    return next((p for p in ((bp or {}).get("props") or [])
                 if p.get("owner") == cid and p.get("weapon")), None)


FIRSTFRAME_TIMEOUT = int(os.environ.get("BOOKREEL_FIRST_FRAME_TIMEOUT", "7200"))


def _comfy_drop(pid):
    """Delete a prompt from ComfyUI's queue, or interrupt it if it is running.
    POST /queue and /interrupt answer with an empty body - never parse them."""
    try:
        with urllib.request.urlopen(COMFY + "/queue", timeout=15) as r:
            q = json.load(r)
        running = any(len(x) > 1 and x[1] == pid for x in q.get("queue_running") or [])
        if running:
            urllib.request.urlopen(urllib.request.Request(COMFY + "/interrupt", data=b"{}",
                                   headers={"Content-Type": "application/json"}), timeout=15).read()
        else:
            urllib.request.urlopen(urllib.request.Request(
                COMFY + "/queue", data=json.dumps({"delete": [pid]}).encode(),
                headers={"Content-Type": "application/json"}), timeout=15).read()
    except Exception:                                       # noqa: BLE001
        pass


def _continuity_ref(prev, cur, prev_take=None):
    """Whether a cold shot should still SEE the previous shot's last frame.

    `_inherit_ratio` says None for a pull-back or a new cast member - a crop
    cannot make a wider frame - but the fight goes on, and a first frame drawn
    from text alone re-dresses everybody. The tail frame goes to Flux as the
    first reference instead, so the people, their clothes and the room carry
    over while the camera is new. Same place, same hour, a hard cut, and at
    least one person in common."""
    if not prev or not cur:
        return False
    if (prev.get("location_id") or "") != (cur.get("location_id") or ""):
        return False
    if (prev.get("time_of_day") or "") != (cur.get("time_of_day") or ""):
        return False
    if any(w in ((cur.get("transitions") or {}).get("in") or "") for w in _SOFT_CUT):
        return False
    pc, cc = set(prev.get("characters") or []), set(cur.get("characters") or [])
    if not (pc & cc):
        return False
    # FLUX COPIES THE REFERENCE'S COMPOSITION, NOT ONLY ITS PEOPLE. Measured on
    # tianlongbabu ch01 (2026-10-04): every close-up that followed a wide was
    # handed the wide's tail as its first reference, and drew the wide again -
    # C01S06 (段誉 alone, a close-up) came out with 8 people after C01S05's
    # duel, C01S11 with 12, C01S19 with 6; 13 of 24 clips failed their head
    # count, most of them this way. And a caption painted on the previous
    # clip rode along too: C01S19's first frame reproduced C01S18's. So the
    # tail is a reference only when the new frame is about the same picture -
    # no more than one size tighter and no more than one person fewer - and
    # never when that clip was flagged for text or for its head count. A
    # close-up is carried by the person's own sheet instead.
    if len(cc) < len(pc) - 1:
        return False
    a = _SHOT_SCALE.get(str((prev.get("camera") or {}).get("shot_size") or "").upper())
    b = _SHOT_SCALE.get(str((cur.get("camera") or {}).get("shot_size") or "").upper())
    if a and b and b < a * 0.7:
        return False
    chk = (prev_take or {}).get("check") or {}
    bad = " ".join(str(x) for x in (chk.get("problems") or []) + (chk.get("remarks") or []))
    if re.search(r"有字|数到|人物不对", bad):
        return False
    return True


def first_frame_refs(slug, shot, bp, plate, cont=""):
    """The pictures a first frame is conditioned on: each named person's front
    sheet (a crowd has none), then the plate. At most three people - past
    that the references crowd the sampler more than they help it."""
    base = os.path.join(PROJECTS, slug, "assets", "images")
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    out = []
    # The previous shot's last frame first: it is the same moment of the same
    # scene, so it outranks every sheet. It also shows the room, so the plate
    # is left out to keep the count down.
    if cont and os.path.isfile(cont):
        out.append(cont)
    # Fighters before watchers: with two person slots, C01S02's went to
    # whoever the 分镜 listed first.
    order = cast_ids(shot, bp)
    fs = fighters(shot, bp)
    # Whoever SPEAKS is who the shot is about: their face first, then the
    # fighters, then the rest. C01S15 (左子穆 talking, 辛双清 and 段誉 in
    # frame) spent its four slots on two sheets, a sword and the plate, and
    # 段誉 was drawn from text alone - a different man in every shot.
    talk = [str(d.get("who")) for d in (shot.get("dialog") or [])
            if isinstance(d, dict) and d.get("who")]
    lead = [i for i in talk + fs if i in order]
    order = list(dict.fromkeys(lead + order))
    # At most FIRSTFRAME_MAX_REFS in all (seven made a first frame take 45
    # min): the people first; ONE weapon, and only in a FIGHT - a sheathed
    # sword at a seated master's hip is said in words, and the slot it took
    # was somebody's face; then the plate (when there is no tail frame).
    combat = is_combat(shot)
    reserve = (1 if combat else 0) + (0 if cont else 1)
    ppl_cap = max(1, FIRSTFRAME_MAX_REFS - reserve)
    # THE FACE IS WHAT A SHEET HAS LEAST OF. A passport sheet is a whole body
    # on white, and at REF_MEGAPIXELS the head in it is ~100 px tall - so the
    # reference carried the costume and the build and almost nothing of the
    # face, and tianlongbabu ch01 (2026-10-06) gave 龚光杰 a clean-shaven
    # young face in C01S01, a moustache in C01S10 and a round middle-aged one
    # in C01S12, all in the same blue. On a shot from the waist up the sheet
    # goes in as its head and shoulders (_bust_crop: about six times the face
    # pixels); a wide shot and a fight keep the whole body, because there the
    # pose and the clothes are what the frame is about.
    tight = BUST_REFS and not combat and str(
        (shot.get("camera") or {}).get("shot_size") or "").upper() in ("MS", "MCU", "CU", "ECU", "BCU")
    for i in order:
        if is_group(by_id.get(i)):
            continue
        p = os.path.join(base, "char_%s.png" % i)
        if os.path.isfile(p):
            out.append(_bust_crop(p) if tight else p)
        if len(out) >= ppl_cap:
            break
    for i in (order[:3] if combat else []):
        wp = weapon_prop(bp, i)
        if wp and os.path.isfile(os.path.join(base, "prop_%s.png" % wp["id"])):
            out.append(os.path.join(base, "prop_%s.png" % wp["id"]))
            break
    if not cont and plate and plate.get("file") and os.path.isfile(os.path.join(base, plate["file"])):
        out.append(os.path.join(base, plate["file"]))
    return out[:FIRSTFRAME_MAX_REFS]


BUST_REFS = os.environ.get("BOOKREEL_BUST_REFS", "1") not in ("0", "", "false")


def _bust_crop(sheet):
    """assets/images/_faces/<sheet>.png: the head and shoulders off a passport
    sheet, for a first frame framed from the waist up. Rebuilt when the sheet
    is newer; on any failure the whole sheet is returned, as before.

    Geometry, not a detector: every sheet here is one standing figure on a
    white board, so the body is the non-white bounding box, the head is its
    top, and head plus shoulders is the top third of it (a standing figure is
    seven to eight heads tall). PIL only - web_server has no numpy."""
    try:
        d = os.path.join(os.path.dirname(sheet), "_faces")
        out = os.path.join(d, os.path.basename(sheet))
        if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(sheet):
            return out
        from PIL import Image
        im = Image.open(sheet).convert("RGB")
        W, H = im.size
        k = 8
        sm = im.resize((max(1, W // k), max(1, H // k)))
        px = sm.load()
        sw, sh = sm.size
        dark = lambda x, y: min(px[x, y]) < 225
        rows = [sum(1 for x in range(sw) if dark(x, y)) for y in range(sh)]
        ys = [y for y, c in enumerate(rows) if c >= max(2, sw * 0.03)]
        if not ys:
            return sheet
        top, bottom = ys[0] * k, (ys[-1] + 1) * k
        body = bottom - top
        if body < H * 0.3:
            return sheet
        # the head's centre line, from the top fifth of the body
        xs = [x for y in range(ys[0], min(sh, ys[0] + max(1, int(body * 0.2 / k))))
              for x in range(sw) if dark(x, y)]
        cx = (sum(xs) / len(xs)) * k if xs else W / 2.0
        ch = body * 0.38
        cw = ch * 0.8
        y0 = max(0, int(top - body * 0.04))
        x0 = int(max(0, min(W - cw, cx - cw / 2)))
        box = (x0, y0, int(min(W, x0 + cw)), int(min(H, y0 + ch)))
        if box[2] - box[0] < 64 or box[3] - box[1] < 64:
            return sheet
        os.makedirs(d, exist_ok=True)
        im.crop(box).save(out + ".tmp.png")
        os.replace(out + ".tmp.png", out)
        return out
    except Exception:                                       # noqa: BLE001
        return sheet


PLATE_ROTATE = os.environ.get("BOOKREEL_PLATE_ROTATE", "0") not in ("0", "", "false")
PLATE_VIEW = os.environ.get("BOOKREEL_PLATE_VIEW", "front")


def _pick_first_frame(slug, shot, lib):
    """The location plate a shot should start from, as a /media path."""
    loc = shot.get("location_id") or ""
    ref = shot.get("first_frame_ref") or ""
    if not loc and not ref.startswith("lo"):
        return None, "no location"
    if not loc:
        loc = ref
    tod = _shot_tod(shot)
    inside = "内景" in (shot.get("slug_line") or "")
    cam = shot.get("camera") or {}
    side = ["_int_front", "_int_left", "_int_right", "_int_back"] if inside else \
           ["_ext_front", "_ext_left", "_ext_right", "_ext_back"]
    # Every shot in a location used to open on the same plate - all twelve of
    # C01S01..S12 started from loca_lo01_日_int_front - which is a large part of
    # why the clips looked like twelve unrelated views of one courtyard. There
    # are nine plates per location; use them. Overhead shots take the overhead
    # plate outright, and the rest rotate by shot number so neighbours differ.
    # This is variety, not staging: the plate cannot know where the camera was
    # meant to stand. The real fix is a first frame generated per shot from its
    # own blocking, with the plate as reference - until then, continuity from
    # the previous shot's tail frame (see _continuous) carries the scene, and
    # this only decides where a scene opens.
    # …and the rotation was the other half of the problem. The nine views
    # are nine separate Flux renders, and they are NOT one building: lo01's
    # four interior views of 练武厅 are four different rooms (a ringed plank
    # floor, a flagstone floor, a raised dais, a row of chairs on a bare
    # wall). Rotated by shot number, tianlongbabu ch01 (2026-10-04) moved
    # the duel between them every cut. A scene is one room: every shot in a
    # location opens on ONE view (BOOKREEL_PLATE_VIEW, default the front),
    # and only an overhead shot takes the overhead plate.
    if "俯" in (cam.get("angle") or "") or "过顶" in (cam.get("height") or ""):
        want = ["_top"] + side
    elif PLATE_ROTATE:
        m = re.search(r"S(\d+)$", shot.get("id") or "")
        k = int(m.group(1)) % len(side) if m else 0
        want = side[k:] + side[:k] + ["_top"]
    else:
        pin = [x for x in side if x.endswith("_" + PLATE_VIEW)]
        want = pin + [x for x in side if x not in pin] + ["_top"]
    want += ["_ext_front", "_int_front", "_top"] if inside else ["_int_front", "_ext_front", "_top"]
    base = os.path.join(PROJECTS, slug, "assets", "images")
    by_key = {i.get("key"): i for i in lib.get("items", [])}

    def try_keys(t):
        for suffix in want:
            k = "loca_%s_%s%s" % (loc, t, suffix)
            it = by_key.get(k)
            if it and os.path.isfile(os.path.join(base, it["file"])):
                return it, k
        return None, None
    for t in [tod] + [x for x, _ in _TOD if x != tod]:
        it, k = try_keys(t)
        if it:
            note = "%s%s" % (k, "" if t == tod else "（无 %s 版，改用 %s）" % (tod, t))
            return ("/media/%s/assets/images/%s" % (slug, urllib.parse.quote(it["file"])),
                    note, dict(it, key=k))
    return None, "no plate for %s" % loc, None


# --------------------------------------------------------------- assembly
# A shot is not one h3 call. h3's cost is frames x pixels and its frame grid is
# 5+17n, so one 15 s render would be 362 frames in a single attention window -
# slow and liable to OOM. Instead a shot is SEG_SECONDS of motion repeated:
# each segment starts from the previous segment's last frame, so the motion is
# continuous across the joins, and ffmpeg concatenates them. Same trick carries
# one shot into the next when the script says they are continuous.
FFMPEG = os.path.join(ROOT, "bin", "ffmpeg")
TTS = "http://127.0.0.1:9100"
# 3.75 s is 90 frames (5+17*5) and four of them are exactly 360 frames, 15.0 s,
# with nothing rendered and then thrown away. Cost per second of finished film
# is the same as the 107-frame rung measured on 2026-09-08 (3.7 GPU-min/s), so
# this buys the clean arithmetic for free.
SEG_SECONDS = float(os.environ.get("BOOKREEL_SEG_SECONDS", "3.75"))
# launch.sh sets this; the fallback is for a hand-started server, and it must
# not be `storyteller` - that is CosyVoice's 262 Hz female demo clip, and a
# server started by hand would quietly narrate the film in a different voice
# from every other run. See voices/README.txt.
VOICE = os.environ.get("BOOKREEL_VOICE", "narrator_male")
# Acting direction for the narrator and for dialog. tts_server has taken an
# `instruct` since it was written (inference_instruct2), and the script has
# written per-line delivery all along, so wiring the two together is the
# obvious fix for narration that comes out level and unstressed.
#
# It is OFF by default because this CosyVoice checkout SPEAKS the instruction
# instead of obeying it. Measured 2026-09-10 on 崇祯六年，秋…（36 chars):
# zero-shot 11.20 s, with a 30-char instruct 23.02 s - the extra 11.8 s is
# almost exactly how long that instruct takes to say. A 14-char dialog line
# went 4.32 s -> 11.78 s and silencedetect shows 8.8 s of unbroken speech
# where the plain read has a natural pause at the comma. Shortening the
# direction to 7 characters still gave 7.56 s in four chunks, so it is not a
# length problem.
#
# The code stays because the wiring is right and the leak is the engine's; set
# these once instruct2 obeys (a newer CosyVoice2, or a build that takes the
# instruct as a separate conditioning input rather than as text to read).
# Until then the real lever on "too plain" is the reference clip itself:
# zero-shot copies the prosody of voices/<name>.wav, and the one that ships is
# CosyVoice's flat demo read.
NARRATION_INSTRUCT = os.environ.get("BOOKREEL_NARRATION_INSTRUCT", "")
DIALOG_INSTRUCT = os.environ.get("BOOKREEL_DIALOG_INSTRUCT", "0") not in ("0", "", "false")
# h3 invents its own audio from the picture, and what it invents is not room
# tone - it is a second person talking, in no language, and each segment
# invents a fresh one, so a four-segment shot had a man muttering four times
# under the narrator. Ducking it to 15% did not help: quiet nonsense speech is
# still nonsense speech. Default 0 drops h3's track entirely and the clip
# carries only the Chinese. Raise it if you ever want the invented ambience
# back, and turn H3_AUDIO_VAE back on in launch.sh so there is one to mix.
AMBIENT_GAIN = float(os.environ.get("BOOKREEL_AMBIENT_GAIN", "0"))


def _has_audio(path):
    exe = _ffmpeg()
    if not exe or not os.path.isfile(path):
        return False
    pr = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, timeout=120)
    return "Audio:" in pr.stderr.decode("utf-8", "replace")


def _ffmpeg():
    if os.path.isfile(FFMPEG) and os.access(FFMPEG, os.X_OK):
        return FFMPEG
    return shutil.which("ffmpeg") or ""


def _ff(args, timeout=900):
    exe = _ffmpeg()
    if not exe:
        raise RuntimeError("没有 ffmpeg：venvs/tools/bin/pip install imageio-ffmpeg")
    pr = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y"] + args,
                        capture_output=True, timeout=timeout)
    if pr.returncode != 0:
        raise RuntimeError("ffmpeg：%s" % pr.stderr.decode("utf-8", "replace").strip()[-300:])


def _duration(path):
    exe = _ffmpeg()
    if not exe or not os.path.isfile(path):
        return 0.0
    pr = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, timeout=120)
    m = re.search(r"Duration: (\d+):(\d\d):(\d\d\.\d+)",
                  pr.stderr.decode("utf-8", "replace"))
    return (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0.0


# A silence this long inside a line is a phrase break worth putting a subtitle
# boundary on. Anything shorter is a breath or a stop consonant.
CUE_GAP_MIN = float(os.environ.get("BOOKREEL_CUE_GAP_MIN", "0.25"))


def _speech_map(path):
    """Where the speech is in an audio file: (lead, tail, internal pauses).

    `lead`/`tail` are the first and last sound - CosyVoice lays 0.15-0.75 s of
    silence in front of every line and a little behind it, and the cue split
    used to spread a line's text across the whole wav, so the first subtitle
    came up as much as three quarters of a second before anything was heard.

    The pauses between those two are what make a subtitle land on the phrase
    it belongs to. Splitting a line by character count alone is close but not
    exact, because a comma is not worth the same time as a syllable and the
    model breathes where it likes; snapping each boundary onto a real gap is.

    ffmpeg's own silencedetect, so it is deterministic and cannot fail in the
    middle of a ninety-minute render. The forced aligner at :9101 was measured
    against this twice (2026-09-13/14) and is not accurate enough to replace
    it: on raw CosyVoice output it put the last sentence of the test line at
    8.55-10.33 s against a measured 10.04-11.54, and on clip audio it was
    1.4 s short. Anything silencedetect does not answer falls back to the
    whole file, which is what the old code assumed anyway.
    """
    dur = _duration(path)
    exe = _ffmpeg()
    if not exe or dur <= 0:
        return 0.0, dur, []
    try:
        pr = subprocess.run(
            [exe, "-hide_banner", "-i", path, "-af",
             "silencedetect=noise=-40dB:d=0.10", "-f", "null", "-"],
            capture_output=True, timeout=120)
    except Exception:                                       # noqa: BLE001
        return 0.0, dur, []
    err = pr.stderr.decode("utf-8", "replace")
    starts = [float(v) for v in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(v) for v in re.findall(r"silence_end: (-?[\d.]+)", err)]
    runs = list(zip(starts, ends))
    lead = ends[0] if starts and ends and starts[0] <= 0.05 else 0.0
    # silencedetect closes the run at EOF, so a trailing silence shows up as a
    # start whose end is the end of the file.
    tail = starts[-1] if starts and ends and ends[-1] >= dur - 0.05 else dur
    if not 0.0 <= lead < tail <= dur:
        return 0.0, dur, []
    # Only gaps long enough to be a PHRASE break. CosyVoice also leaves 0.1-0.2 s
    # holes inside a word - a stop consonant closure, a breath - and snapping a
    # subtitle boundary onto one of those puts it mid-word, which is the thing
    # this is meant to prevent. The pauses it writes at punctuation measured
    # 0.30-0.65 s on the test line, the ones inside words 0.13-0.22.
    inner = [(a, b) for a, b in runs
             if a > lead + 1e-6 and b < tail - 1e-6 and (b - a) >= CUE_GAP_MIN]
    return lead, tail, inner


def _speech_span(path):
    """(first sound, last sound). Kept for callers that do not need pauses."""
    lead, tail, _ = _speech_map(path)
    return lead, tail


def _dimensions(path):
    """(width, height) of a video, or None. Same trick as _duration: read it
    off ffmpeg's own stderr rather than requiring ffprobe, which bin/ does not
    necessarily ship."""
    exe = _ffmpeg()
    if not exe or not os.path.isfile(path):
        return None
    pr = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, timeout=120)
    m = re.search(r"Video:.*?, (\d+)x(\d+)[ ,]",
                  pr.stderr.decode("utf-8", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _last_frame(mp4, png):
    """The clip's final frame: what the next segment starts from."""
    _ff(["-sseof", "-0.15", "-i", mp4, "-frames:v", "1", "-q:v", "2", png], timeout=180)
    if not os.path.isfile(png):
        raise RuntimeError("取不到末帧 %s" % os.path.basename(mp4))


def _concat_stream(parts, out):
    lst = out + ".concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for q in parts:
            f.write("file '%s'\n" % os.path.abspath(q).replace("'", "'\\''"))
    try:
        _ff(["-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    finally:
        try:
            os.unlink(lst)
        except OSError:
            pass


# A voiced shot's picture ends this long after the line (dialog) or the
# narration (VIDEO_HANDLE_SECONDS). 0 keeps the whole rendered grid.
VOICE_TAIL_TRIM = os.environ.get("BOOKREEL_VOICE_TAIL_TRIM", "1") not in ("0", "", "false")
DIALOG_TAIL_SECONDS = float(os.environ.get("BOOKREEL_DIALOG_TAIL", "0.8"))


def _cut_picture(src, dst, secs):
    """The first `secs` of a picture, re-encoded to its own parameters.

    Re-encodes (a cut on a keyframe is not where the line ends), picture
    only: the mux lays the sound on afterwards and still copies the video."""
    par = _stream_params(src)
    args = ["-i", src, "-t", "%.3f" % float(secs), "-an",
            "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "16",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    if par:
        args += ["-r", "%g" % par[2]]
        if par[3]:
            args += ["-video_track_timescale", str(par[3])]
    _ff(args + [dst], timeout=900)
    if not os.path.isfile(dst):
        raise RuntimeError("没写出来")
    return dst


def _trim_to_format(src, dst, want):
    """Crop the multiple-of-32 padding rows off a finished clip.

    A canvas side must be a multiple of 32 and 1080 is not one, so 16:9 renders
    1920x1088 and the delivered frame is the middle 1920x1080 of it. This trims
    ONLY that padding: if the rendered canvas is more than 31 rows or columns
    away from the target it is a genuinely different (usually smaller) canvas -
    somebody lowered H3_MAX_PIXELS to buy wall clock - and scaling it up here
    would invent pixels, not detail. In that case the clip goes out at what was
    actually rendered, which is what every earlier clip on disk did.

    Returns the path to use. Re-encodes, so it is called once on the joined
    picture and never on the mux, which must keep -c:v copy.
    """
    m = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", str(want or ""))
    if not m:
        return src
    tw, th = int(m.group(1)), int(m.group(2))
    got = _dimensions(src)
    if not got:
        return src
    gw, gh = got
    if (gw, gh) == (tw, th):
        return src
    if not (tw <= gw < tw + 32 and th <= gh < th + 32):
        return src
    _ff(["-i", src, "-vf", "crop=%d:%d:%d:%d" % (tw, th, (gw - tw) // 2, (gh - th) // 2),
         "-c:v", "libx264", "-preset", "medium", "-crf", "16",
         "-pix_fmt", "yuv420p", "-an", dst])
    return dst if os.path.isfile(dst) else src


# The script's shot_size codes mean nothing to a video model - the same lesson
# as 油灯 and 木格窗 in CLAUDE.md: name the thing in plain words. "ELS" got no
# framing at all, which is how 79 of 108 shots went out with no camera
# instruction and every cut came back looking like the last one.
# WHAT THE PICTURE SHOWS, never what the camera is doing. Measured on the
# regenerated C01S01, 2026-09-18: the camera block was rewritten from labels
# (近景，平视，机位齐眼高…) into prose (这是一个平视的大远景，镜头在眼睛的高度…)
# and h3 painted MORE of it - six captions in one clip: 这是一个平视的大远,
# 镜叁在眼睛的高度, 前国景都清晰, 图上三分法. So it was never labels versus
# prose. h3 paints anything that is not a DEPICTABLE THING IN THE WORLD: a
# shot size, an eye-line height, a rule of thirds cannot be drawn, so the only
# way to put them in a picture is to write them. The scene prose has never
# been painted, because it can be drawn.
#
# Every entry below is a sentence about what a viewer sees, with no apparatus
# noun in it: no 镜头, no 机位, no 景别 name, no 构图, no 景深.
_SIZE_WORDS = {"ELS": "人在画面里很小，四周的环境占满画面",
               "LS": "整个人连同周围的环境都在画面里",
               "MLS": "人从膝盖往上在画面里",
               "MS": "人从腰往上在画面里",
               "MCU": "人从胸口往上在画面里",
               "CU": "画面里是这个人的脸",
               "ECU": "画面里只有脸上的一小块",
               "BCU": "画面里只有脸上的一小块"}
_ANGLE_WORDS = {"平视": "", "俯角": "从高处往下看", "仰角": "从低处往上看",
                "鸟瞰": "从正上方往下看", "低角贴地": "贴着地面往上看",
                "斜角": "画面微微倾斜", "过肩": "越过另一个人的肩膀看过去",
                "主观": "像是用这个人的眼睛在看"}
# Each of these is a CLAUSE about the picture, not a label. The block used to
# read 近景，平视，机位齐眼高，固定机位，浅景深，背景虚化，中心构图，面部居中 -
# eight labels on commas, in the first position of every prompt - and h3
# painted its first token: 近景苏州汉丙绷绷 across the bottom of C01S12, the
# camera label welded to the scene prose behind it. A label is a caption; a
# sentence is a thing to film. Same lesson as the anchor and the anatomy line.
# Where the VIEWER stands. Eye level is what a picture looks like by default,
# so it says nothing; only the unusual heights are worth a clause.
_HEIGHT_WORDS = {"齐眼": "", "腰高": "从腰那么高的地方看过去",
                 "地面": "贴着地面看过去", "过顶": "从头顶上方往下看",
                 "膝高": "从膝盖那么低的地方看过去"}
# Stated as what the camera does, never as what it does not - a negation puts
# the noun in the picture (CLAUDE.md), and "不移动" is still 移动 to the model.
# A still frame is what a picture does by default, so 固定 says nothing at
# all - and saying it cost six painted captions. The rest are what happens to
# the VIEW, not to the apparatus.
_MOVE_WORDS = {"固定": "", "推": "画面慢慢靠近", "拉": "画面慢慢拉远",
               "俯仰": "视线上下移动", "升降": "视线整体升高或降低",
               "手持": "画面像手持那样轻微晃动", "横移": "画面平移过去",
               "摇": "视线横着扫过去", "斯坦尼康": "画面跟着人平稳移动"}
_DOF_WORDS = {"浅": "背景虚成一片", "中": "", "深": "远处近处都清楚"}
_SPEED_WORDS = {"慢": "缓慢", "极慢": "极缓慢", "中": "中速", "快": "快速"}
# Composition is the field that most directly stops two shots in one room from
# framing up the same way, but it is free prose; past this length it starts
# arguing with the scene description instead of placing the camera.
CAMERA_COMPOSITION_MAX = int(os.environ.get("BOOKREEL_CAMERA_COMPOSITION_MAX", "60"))
# Trade vocabulary: none of it can be drawn, all of it can be written. The
# composition field is free prose and half of it is this - 三分法, 左1/3线,
# 中心构图, 引导线 - which is what 图上三分法 was.
_JARGON_RX = re.compile(r"三分|构图|引导线|分线|对角线|框式|留白|画幅|中心线|"
                        r"机位|镜头|景深|焦点|焦段|轴线|视平|虚化|1/3|2/3|1／3")
# OFF. Three rewrites of this block were painted into the picture in three
# days - as labels (近景，平视，机位齐眼高), as prose (这是一个平视的大远景),
# and as plain viewer description (远处近处都清楚, measured on C01S01
# 2026-09-19 at 7 s, 10 s and 14.5 s). The pattern is not punctuation and not
# vocabulary: h3 paints STATEMENTS ABOUT THE PICTURE, whatever words they are
# made of, because a statement about the picture cannot be a thing inside it.
# What is left to carry framing is what always carried it better anyway - the
# first frame, which IS the framing, and the previz control video, which pins
# the geometry now that it is cropped to match. Set BOOKREEL_CAMERA_IN_PROMPT=1
# to put it back and watch it be written on the screen again.
CAMERA_IN_PROMPT = os.environ.get("BOOKREEL_CAMERA_IN_PROMPT", "0") not in ("0", "", "false")


def _plain(v):
    """Drop an ASCII gloss like "(lock-off)" or "(OTS)", keep a Chinese one."""
    v = str(v or "").strip()
    v = re.sub(r"[(（]\s*[\x00-\x7f]+\s*[)）]", "", v)
    return v.strip().strip("，。").strip()


# ----------------------------------------------------------------------
# what h3 is actually told
#
# MiniMax-H3 PAINTS label-shaped text into the picture. Measured on xuemang
# chapter 1, 2026-09-16: 17 of 20 clips carried burned-in Chinese captions, and
# every one of them was a verbatim piece of the prompt's head or tail --
# "江南·苏州府及运河沿线", the bare name out of "画中人物：沈墨", "明代士大夫
# 文化、漕运官营制度", "士农工商". The model's OWN scene prose was never
# painted; it was filmed. The difference is shape, not subject: a label
# ("背景设定：文化：…；社会：…") reads as an on-screen title, a sentence reads
# as a thing to depict.
#
# So the anchor still goes in - era, place, ethnicity, gender and age are
# exactly what ch01-rendered-as-a-European-woman bought us - but as prose, with
# no colons, no enumerations, no proper names and no "·" title separators.
# A negative prompt is not available to fix this: the turbo LoRA runs at
# cfg 1.0 and h3's negative branch is a ConditioningZeroOut, so anything put
# there is multiplied by nothing.
_LABEL_TAIL = re.compile(r"\s*背景设定[：:].*$", re.S)
_LABEL_HEAD = re.compile(r"^.*?画中人物[：:][^。]*。\s*")


def _scene_text(shot):
    """The model's own scene description, without the stored label block."""
    raw = (shot.get("video_prompt_raw") or "").strip()
    if raw:
        return raw
    # Older scripts kept only the anchored text; peel the label head and tail.
    s = (shot.get("video_prompt") or shot.get("slug_line") or "").strip()
    s = _LABEL_TAIL.sub("", s)
    s = _LABEL_HEAD.sub("", s)
    return s.strip()


def _people_clause(shot, bp):
    """Who is in frame, as a NOUN PHRASE naming them.

    The count still has to be said - the model duplicated its subject
    repeatedly, C01S02 drew 沈墨 twice at one desk and C01S08 drew 阿蘅 twice -
    but every phrasing that said it as a STATEMENT has been painted across the
    picture. 画面中只有一个人 was, so it was moved off the frame and onto the
    place; 这里只有一个人 then was too, legibly, across the bottom of C01S01 on
    2026-09-21, with 二十五罗罗鬣汝汉人 garbled underneath it - the attribute
    list from this same clause.

    The LLM's own scene prose has never once been painted in this project's
    history, and it carries the same facts: 明代苏州汉人青年男子，靛蓝粗布长衫，
    坐石凳上搁笔抬眼. The difference is not punctuation and not register, both
    of which were tried - it is that prose NAMES A PERSON and a statement
    ASSERTS SOMETHING ABOUT THE PICTURE, and the only way a picture can contain
    an assertion is to write it down.

    So the count rides inside the noun phrase, where it is part of the thing to
    draw: 一个二十五岁男子 for one, 一个…和一个… for two, and 独自一人 for the
    exclusivity that 只有 used to carry - a man being alone is a thing you can
    draw, "there is only one person here" is not. The backstop against a
    duplicated subject is what it always really was anyway: the pinned first
    frame, the control video, and clip_headcount on the finished clip.
    """
    w = (bp.get("world") or {})
    people = _plain(w.get("people")) or ""
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    # The same list the head count is checked against (cast_ids).
    ids = cast_ids(shot, bp)
    who, crowd = [], []
    # Only the DUELLISTS have a blade out. Every armed character in a fight
    # shot was told 手里握着一把出鞘的长剑, so the seated sect master watching
    # from his chair was drawn sword in hand too. Seated/kneeling in the
    # staging's first beat means watching; at most two draw.
    st0 = (((shot.get("staging") or {}).get("beats") or [{}])[0]
           if isinstance(shot.get("staging"), dict) else {}) or {}
    standing = [i for i in ids if str((st0.get(i) or {}).get("posture") or "stand") not in ("sit", "kneel", "lie")]
    # the fighters by name first (C01S02: 段誉 was standing and so drew too)
    fw = fighters(shot, bp)
    drawn = set(fw or (standing or ids)[:2])
    for i in ids:
        c = by_id[i]
        if is_group(c):
            crowd.append("一群%s" % _plain(c.get("name")).rstrip("们"))
            continue
        g = _plain(c.get("gender"))
        sex = "男子" if g.startswith("男") else ("女子" if g.startswith("女") else "人")
        who.append(_age_words(c.get("age")) + sex)
    n = len(who)
    if not n and not crowd:
        return ""
    # 一个二十五岁男子，独自一人，汉人，东亚面孔。- the same shape as the
    # scene prose beside it, which has never been painted. A crowd is a noun
    # phrase too (一群女弟子), and nobody in a crowd is alone.
    # A FIGHT names its fighters and puts everyone else on the side, empty-
    # handed: C01S02 drew four people fighting when the cast was two fighters
    # plus four onlookers, all described alike (2026-09-30).
    # EACH PERSON WEARS THEIR OWN CLOTHES. The clause used to say
    # 人物穿深蓝色交领劲装、青绿色交领劲装 - two colours for "the people" - and
    # h3 shared them out as it liked: tianlongbabu C01S01 (2026-10-04) opened
    # blue against green and by its second second both duellists were green.
    # The costume is part of the noun phrase now, so it belongs to one body:
    # 一个穿深蓝色交领劲装的三十五岁男子.
    def one(i, dress=True):
        c = by_id[i]
        g = _plain(c.get("gender"))
        cos = re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                                             filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
        wear_ = ("穿%s的" % cos) if (dress and cos and len(cos) <= 24) else ""
        return "一个" + wear_ + _age_words(c.get("age")) + (
            "男子" if g.startswith("男") else ("女子" if g.startswith("女") else "人"))
    hair = "发型是这个时代的样式。"
    fw = fighters(shot, bp)
    if fw and len(ids) > len(fw):
        duel = "和".join(one(i) for i in fw)
        watch = [one(i, dress=False) for i in ids if i not in fw and not is_group(by_id[i])] + crowd
        num = {1: "一个人", 2: "两个人", 3: "三个人"}.get(len(fw), "%d个人" % len(fw))
        return ("场中央只有%s在交手：%s，手里握着出鞘的剑%s。%s"
                "四周是观战的人：%s，他们双手空着，坐着或站着看，一动不动。"
                % (num, duel, ("，" + people) if people else "", hair,
                   "、".join(watch)))
    named = [i for i in ids if not is_group(by_id[i])]
    who_txt = "和".join([one(i, dress=len(named) <= 3) for i in named] + crowd)
    alone = "，独自一人" if n == 1 and not crowd else ""
    wear = hair if len(named) <= 3 else look.wear_clause(bp, [by_id[i] for i in ids])
    return "%s%s%s。%s" % (who_txt, alone, ("，" + people) if people else "", wear)


def _body_clause(shot):
    """Hands and limbs, said as what they are.

    The other half of the duplication problem, and the one a negative prompt
    would normally take: six fingers, a third arm, a second copy of the same
    person. There is no negative here - the turbo LoRA runs at cfg 1.0 and
    h3's negative branch is a ConditioningZeroOut - so it is said positively,
    the way prop counts are said on a reference sheet ("一把刀就是一把刀").
    Short, and at the end: it is a correction to a picture the rest of the
    prompt has already described, not a subject.
    """
    if not (shot.get("characters") or []):
        return ""
    return look.always_positive("shot") + "。"


def spoken(shot):
    """The dialog lines said ALOUD. A line with `inner: true` is a thought in
    the character's own voice (2026-09-30: "旁白更多应该是心里话") - heard, but
    not said: the mouth stays shut, no lip-sync, no cold-start lead."""
    return [d for d in ((shot or {}).get("dialog") or [])
            if isinstance(d, dict) and not d.get("inner")]


def _speaker_clause(shot, bp):
    """WHO talks, said as a person: 穿深蓝色交领劲装的三十五岁男子在说话，嘴唇
    随话语开合；其他人嘴唇闭合。 Nothing in the prompt used to name the speaker,
    so in a two-shot h3 either moved nobody's mouth (C01S13/14, 2026-10-06:
    every face's lips flat through the line) or everybody's. One person,
    identified by the clothes - the thing the picture can show. Empty when the
    cast is one (the only face talks anyway) or the speaker is not known."""
    if not bp or not shot:
        return ""
    who = [str(d.get("who")) for d in spoken(shot) if d.get("who")]
    ids = [str(i) for i in (shot.get("characters") or [])]
    if not who or len(ids) < 2:
        return ""
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    c = by_id.get(who[0])
    if not c or is_group(c):
        return ""
    cos = re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                                         filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
    if not cos or len(cos) > 24:
        return ""
    g = _plain(c.get("gender"))
    sex = "男子" if g.startswith("男") else ("女子" if g.startswith("女") else "人")
    return "穿%s的%s%s在说话，嘴唇随话语开合；其他人嘴唇闭合。" % (cos, _age_words(c.get("age")), sex)


def _person_phrase(c):
    """穿深蓝色交领劲装的三十五岁男子 - a person said by the clothes, which is
    the one thing about them the picture can show. "" when the costume is
    missing or too long to be a noun phrase."""
    if not c or is_group(c):
        return ""
    cos = re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                                         filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
    if not cos or len(cos) > 24:
        return ""
    g = _plain(c.get("gender"))
    sex = "男子" if g.startswith("男") else ("女子" if g.startswith("女") else "人")
    return "穿%s的%s%s" % (cos, _age_words(c.get("age")), sex)


# How a line names the person it is said TO: the surname plus a form of
# address (辛师妹, 左师兄, 褚师弟, 段世兄, 马五哥, 左师), or the whole name.
_ADDRESS_TAIL = (r"[一-鿿]?(?:师妹|师兄|师弟|师侄|师姐|师叔|师伯|师父|师傅|师|世兄|世弟|世侄|"
                 r"兄弟|兄|弟|哥|姐|妹|姑娘|公子|先生|前辈|老|掌门|大侠|少侠|相公|夫人|小姐|"
                 r"姑姑|叔|伯|爷|婆婆|大师|道长|真人|庄主|帮主|堂主|将军|大人|居士)")


def _addressed(line, bp, exclude=None):
    """The character a line speaks to, by the way it names them; earliest
    mention wins. None when nobody is named."""
    best = None
    for c in ((bp or {}).get("characters") or []):
        cid = str(c.get("id") or "")
        if not cid or cid == exclude or is_group(c):
            continue
        nm = re.sub(r"[（(].*?[）)]", "", str(c.get("name") or "")).strip()
        if len(nm) < 2 or not re.match(r"[一-鿿]", nm):
            continue
        for pat in (re.escape(nm), re.escape(nm[0]) + _ADDRESS_TAIL):
            m = re.search(pat, line or "")
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), cid)
    return best[1] if best else None


def _listener(shot, bp):
    """(who the first spoken line is said to, whether they are in the shot).

    The 分镜 writes who speaks and never who listens; the listener is worked
    out here, in code, the way the world context is: named in the line
    (辛师妹 → ch04), else the other voice in the shot's dialog, else the only
    other named person in frame, else the nearest by staging. (None, False)
    when the speaker is alone and names nobody."""
    sp = spoken(shot)
    if not sp or not bp:
        return None, False
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    who = str(sp[0].get("who") or "")
    if who not in by_id or is_group(by_id[who]):
        return None, False
    ids = [i for i in cast_ids(shot, bp) if i != who and not is_group(by_id[i])]
    hit = _addressed(str(sp[0].get("line") or ""), bp, exclude=who)
    if hit:
        return hit, hit in ids
    others = [str(d.get("who")) for d in sp
              if str(d.get("who")) != who and str(d.get("who")) in by_id and not is_group(by_id[str(d.get("who"))])]
    if others:
        return others[0], others[0] in ids
    if len(ids) == 1:
        return ids[0], True
    if len(ids) > 1:
        st0 = (((shot.get("staging") or {}).get("beats") or [{}])[0]
               if isinstance(shot.get("staging"), dict) else {}) or {}
        me = (st0.get(who) or {}).get("pos")
        if isinstance(me, list) and len(me) >= 2:
            def _d(i):
                q = (st0.get(i) or {}).get("pos")
                if not (isinstance(q, list) and len(q) >= 2):
                    return 1e9
                try:
                    return (float(q[0]) - float(me[0])) ** 2 + (float(q[-1]) - float(me[-1])) ** 2
                except (TypeError, ValueError):
                    return 1e9
            return min(ids, key=_d), True
        return ids[0], True
    return None, False


def _screen_side(shot, a, b):
    """左边 / 右边 / "": where b stands as the viewer sees it, relative to a.

    From the staging when both are placed: the camera looks at a from the
    way a faces (yaw 0 = +Z, +X is right when looking in from the entrance,
    stage.py), so its right-hand vector is (-cos yaw, sin yaw) and the sign
    of b-a along it is the side. Else from the 分镜's own screen_direction
    (面向画左). C01S23: 左子穆 at (5,1) facing 200, 段誉 at (-5,2) → 左边,
    which is what the script wrote."""
    st0 = (((shot.get("staging") or {}).get("beats") or [{}])[0]
           if isinstance(shot.get("staging"), dict) else {}) or {}
    pa, pb = (st0.get(a) or {}).get("pos"), (st0.get(b) or {}).get("pos")
    try:
        if isinstance(pa, list) and isinstance(pb, list) and len(pa) >= 2 and len(pb) >= 2:
            yaw = math.radians(float((st0.get(a) or {}).get("yaw") or 0.0))
            rx, rz = -math.cos(yaw), math.sin(yaw)
            dx, dz = float(pb[0]) - float(pa[0]), float(pb[-1]) - float(pa[-1])
            v = dx * rx + dz * rz
            if abs(v) > 0.3:
                return "右边" if v > 0 else "左边"
    except (TypeError, ValueError):
        pass
    sd = _plain(((shot.get("camera") or {}) if isinstance(shot.get("camera"), dict) else {}).get("screen_direction"))
    m = re.search(r"(?:面向|朝|看向|望向|转向)画(左|右)", sd or "")
    return (m.group(1) + "边") if m else ""


def _facing_clause(shot, bp):
    """WHO TALKS TO WHOM, as two bodies turned to each other.

    tianlongbabu ch01 (2026-10-09, "when chat, they should be face to face,
    but turn out not"): C01S14 had 褚 answering 龚光杰 with both men square
    to the lens, C01S15/S16/S21 had 左子穆 addressing 辛双清 straight into the
    camera with her beside or behind him, C01S16 three people in a row. The
    script knew: staging has a yaw per person and blocking/screen_direction
    say who faces where - and none of it reached a prompt (previz is for the
    person's eyes only, blocking is read by nothing). So the listener is
    resolved in code (`_listener`) and said as a thing to draw: two people
    面对面, half-turned, eyes on each other's face; a listener out of frame
    gives the speaker a side to turn to. Goes into the first frame (h3 keeps
    frame 0's composition) and every segment. Nothing on a fight (the fight
    rules own the geometry) or when nobody is listening."""
    if not shot or not bp or is_combat(shot):
        return ""
    sp = spoken(shot)
    if not sp:
        return ""
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    who = str(sp[0].get("who") or "")
    a = _person_phrase(by_id.get(who))
    if not a:
        return ""
    lid, inside = _listener(shot, bp)
    b = _person_phrase(by_id.get(lid)) if lid else ""
    # In a close shot "面对面" is two profiles: C01S14 (CU, 2026-10-09) came
    # back nose to nose in full profile, and LatentSync left a profile mouth
    # untouched (相关 -0.28 → -0.28). A close shot of a conversation is an
    # over-the-shoulder: the listener's back shoulder in the near corner,
    # the speaker beyond it turned to them with most of the face and both
    # eyes showing - a three-quarter face, which is what the lip-sync model
    # can redraw. The same words the jargon table uses for 过肩镜头.
    cam = (shot.get("camera") or {}) if isinstance(shot.get("camera"), dict) else {}
    tight = str(cam.get("shot_size") or "").strip().upper() in ("MCU", "CU", "BCU", "ECU")
    if lid and inside and b and b != a:
        if tight:
            return ("越过%s的肩膀看过去，画面近处一角只有这个人的后肩和后脑；%s在肩膀那边面朝着对方，"
                    "露出大半张正脸、两只眼睛都看得见，目光看着对方的脸。" % (b, a))
        return "%s和%s面对面，两人都半侧着身，目光看着对方的脸。" % (a, b)
    if lid and not inside:
        side = _screen_side(shot, who, lid)
        if side and tight:
            return "%s的脸朝%s微微侧过去，露出大半张正脸、两只眼睛都看得见，目光望着%s远处。" % (a, side, side)
        if side:
            return "%s半侧着身，脸转向%s，目光望着%s远处。" % (a, side, side)
    return ""


def _crowd_bg_clause(shot, bp):
    """The hall is still full when the camera moves in.

    tianlongbabu ch01 (2026-10-09): every close shot of the chapter opened on
    an EMPTY hall - rows of chairs, a weapon rack, nobody - behind a person
    who in the wide a second earlier was standing in a crowd of forty
    (C01S03, S06, S07, S11, S12, S14, S17, S18, S19, S23, S24, S25: twelve
    of twenty-four clips). A close-up cannot inherit the wide's tail frame
    (its composition would be copied, cutting.md), so it opens on the plate,
    and the plate is the room with nobody in it; the 分镜 says 背景虚化 and
    names no one. So when the SCENE has a crowd (`_scene_crowd`, tagged on
    the shot by the render loop) and this shot does not list it, the
    onlookers are said as what stands behind the subject, with the crowd
    character's own clothes. Into the first frame and every segment."""
    if not shot or not bp or not shot.get("_scene_crowd") or _has_crowd(shot, bp):
        return ""
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    grp = shot.get("_scene_group") or ""
    c = by_id.get(str(grp)) or {}
    cos = ""
    if c:
        cos = re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                                             filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
    folk = ("一排排穿%s的人" % cos) if cos and len(cos) <= 24 else "一排排的人"
    return "身后远处站着%s和看热闹的宾客，厅里站满了人，他们都只是站着看。" % folk


def _tag_scene(shot, scene, bp):
    """Mark on the in-memory shot what its scene holds (never written back)."""
    if not shot:
        return shot
    try:
        shot["_scene_crowd"] = _scene_crowd(shot, scene, bp)
        by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or []) if c.get("id")}
        base = lambda sh: (str(sh.get("location_id") or ""),
                           re.split(r"[（(]", str(sh.get("slug_line") or ""), 1)[0].strip())
        me = base(shot)
        # the crowd the scene lists most often (ch17 无量剑宗弟子 in the
        # hall, not ch15 女弟子们 who stand in one shot of it)
        seen = {}
        for o in (scene or []):
            if base(o) != me:
                continue
            for i in (o.get("characters") or []):
                if str(i) in by_id and is_group(by_id[str(i)]):
                    seen[str(i)] = seen.get(str(i), 0) + 1
        shot["_scene_group"] = max(seen, key=seen.get) if seen else ""
    except Exception:                                   # noqa: BLE001
        pass
    return shot


def _mouth_clause(shot, quiet=False, bp=None):
    """Lips closed when nobody in this shot is speaking RIGHT NOW.

    The narration is a voice-over: nobody in the picture is saying it. The
    model animates a talking mouth anyway, and a mouth moving against a
    narrator's voice is the thing that reads as "dubbed". Shots that DO carry
    dialog are left alone - the mouth should move there, and lip-sync is not
    something this pipeline can promise either way.

    But "carries dialog" is not "talks for its whole length". C01S08 is 11.25 s
    of picture over 7.24 s of whispered lines (0.00-3.85, 4.00-7.13), so its
    third segment - the last 3.75 s - has no speech in it at all. Told nothing,
    h3 mouthed words there against silence: the same defect as the voice-over
    mouth, inside a shot that does speak. `quiet` is the caller's answer for
    THIS segment.
    """
    # Lips only. It used to end 安静地做动作 - "does the action QUIETLY" - on
    # every silent shot, fights included: tianlongbabu C01S02 is two men
    # trading sword cuts and h3 was told, beside the people clause, to do it
    # calmly. It did (2026-09-28: a posed, slow cross of blades, blank faces).
    # The mouth is the only thing this clause is for.
    if quiet or not spoken(shot):
        return "人物嘴唇闭合。"
    if quiet is None:
        # The line ENDS inside this segment. C01S07 and C01S14 are 3.75 s of
        # picture over 0.74 s of speech in a single segment, so there is no
        # boundary to gate on and three quarters of those clips were a mouth
        # moving against nothing. Said as what the picture does, in order,
        # never as a negation.
        return _speaker_clause(shot, bp) + "人物说完这句后嘴唇闭合。"
    return _speaker_clause(shot, bp)


# A fight is not a conversation with swords. Detected off the beats' own
# sounds first (the 分镜 files every blow under 击打 / 刀击) and the action
# verbs second; 长剑撑地 is a sword and not a fight, so the bare noun is not
# enough.
_COMBAT_SFX = {"刀击"}
# 击打 is NOT enough on its own: the 分镜 files a sword planted on the floor
# (长剑在地面一撑) under 击打, and C01S13/14 - a bleeding leg, a man getting up
# - were read as fights and handed 两人你来我往 and a 3x clock.
_COMBAT_RX = re.compile(r"(?<![刺带讽])刺(?!绣|眼|耳|骨|痛|激)|劈|砍|削(?!瘦)|上撩|反撩|横扫|对拆|拆招|拆到|过招|交手|"
                        r"格挡|挡格|横格|格开|磕开|双剑相|出掌|拍出|一掌|拍向|击向|出拳|飞踢|踢向|扑向|圈转")


def is_combat(shot):
    shot = shot or {}
    beats = [b for b in (shot.get("action_beats") or []) if isinstance(b, dict)]
    # A clash takes two blades: a lone youth planting his sword on the floor
    # was filed under 刀击 too (C01S08, 2026-09-29).
    two = len(shot.get("characters") or []) >= 2
    for b in beats:
        if two and _COMBAT_SFX & set(foley.normalize(b.get("sfx") or [])):
            return True
    snd = shot.get("sound") or {}
    if two and isinstance(snd, dict) and _COMBAT_SFX & set(foley.normalize(
            (snd.get("sfx") or []) + (snd.get("foley") or []))):
        return True
    txt = "".join(str(shot.get(k) or "") for k in ("action", "video_prompt_raw"))
    txt += "".join(str(b.get("do") or "") for b in beats)
    return bool(_COMBAT_RX.search(txt))


_ORBIT_RX = re.compile(r"环绕|arc|360|绕拍|旋转镜头|orbit", re.I)


def is_orbit(shot):
    """The 分镜 asked for the camera to go round the fight (环绕(arc) 360度)."""
    cam = (shot or {}).get("camera") or {}
    mv = " ".join(str(cam.get(k) or "") for k in ("movement", "movement_motivation")) \
        if isinstance(cam, dict) else ""
    return is_combat(shot) and bool(_ORBIT_RX.search(mv))


# Where the two bodies GO in THIS segment, one movement per segment. The old
# clause listed every movement there is (腾空跃起、旋身飞踢、扫堂腿、滑步、绕到
# 对方身后、一圈圈游斗、满场游走…) on every segment, and h3 drew the one it
# knows best from that list: two men running round each other trading
# overhead chops, three segments running (tianlongbabu C01S02/S04,
# 2026-10-14: "对战招数过于单调，只有劈，而且只能转圈跑"). The technique is
# the beat's business (`filmwords.sword_paths` spells it as the blade's
# path); this only says the footwork, and a different one each segment.
_FIGHT_TRAVEL = (
    "一人弓步冲刺直逼过去，另一人侧身滑步让开再反扑，两人斜穿过整片场地交换了位置",
    "一个背影向场地深处扑去、另一个迎面冲近，两人一远一近错开，不在同一条横线上",
    "一人绕到对方身后，对方旋身接招，两人从近处打到场地深处又打回来",
)


def _energy_clause(shot, i=0):
    """Effort, said as what bodies and faces DO - never as a statement about
    the picture (that is what gets painted). Only on a fight: a scholar at a
    desk with his jaw clenched is a different film. `i` is the segment: the
    footwork changes from one to the next."""
    if not is_combat(shot):
        return ""
    out = ("出手迅猛有力，眉头紧锁，咬紧牙关，衣襟和发梢随动作猛地甩起，"
           "兵刃挥过带出一道残影，脚下扬起细尘。")
    # More than one move: h3 does ONE thing per segment unless the thing it is
    # told is a flurry ("I don't want single move, I want more action",
    # 2026-09-28). Said as what two people DO, before the effort.
    if len(shot.get("characters") or []) >= 2:
        # …and they MOVE. C01S08 (2026-09-29) had one leap and then two
        # segments of both men planted in their own half of the frame, trading
        # blows in place: "they can't just stand still, they need move, jump,
        # swing". Footwork said as what their bodies do, like the rest.
        # …and not along ONE LINE. 满场游走 alone came back as two profiles
        # sliding left and right on a rail across the frame (C01S01/S02,
        # 2026-09-30: "move around in the ground, don't just stay in one
        # line"). The floor has depth: say it as where their bodies GO -
        # circling, one nearer and one farther, crossing on the diagonal.
        out = ("两人你来我往，有攻有守，一个进攻、另一个格挡后立刻反击，刺、抹、点、撩、扫各不相同，"
               "没有一招重复，剑招之间夹着一记旋身飞踢或扫堂腿，脚下一刻不停，"
               + _FIGHT_TRAVEL[int(i or 0) % len(_FIGHT_TRAVEL)] + "，"
               "内力激荡，衣袍无风自鼓，掌风剑风把地上尘土震成一圈荡开，四周空气像热浪一样扭曲，" + out)
    # The ORBIT, when the 分镜 asks for one. A camera sentence has been painted
    # as a caption every time it was tried, so it is said as what the viewer
    # SEES move - the room going round behind the two of them - which is a
    # thing in the world, and the caption guard is the backstop.
    if is_orbit(shot):
        # (it used to end 两人始终在画面中央 - a statement about the frame)
        out = "四周的厅柱、窗格和观战的人群绕着交手的两人一圈圈转过去，" + out
    # The blade in the hand is a Chinese one: grip and guard, said as the
    # object (the first frame carries the rest).
    look_ = filmwords.blade_short(filmwords.blade_noun(
        "".join(str(shot.get(k) or "") for k in ("action", "video_prompt_raw"))))
    return out + (look_ + "。" if look_ else "")


# `performance` is the 分镜's most specific column - 少年眼神凌厉，手腕翻转极快；
# 汉子肩沉腰稳，挡格时右臂肌肉绷起 - and until 2026-09-28 NOTHING READ IT. Every
# face h3 drew came from the scene prose alone, which rarely says what a face is
# doing, so every face was the reference sheet's 中性表情 carried through the
# clip. It is prose about eyes, brows, mouths, hands and shoulders: drawable,
# the shape that has never been painted.
PERFORMANCE_MAX = 110
_FACE_RX = re.compile(r"眼|眉|嘴|唇|脸|面|神|笑|怒|咬|牙|额|汗|鼻|颊|瞪|瞥|皱")


# A face told it is frozen is drawn frozen. The 分镜 wrote 面部肌肉完全静止 and
# 仍无其他动作 for a woman holding her anger in (C01S03), which is a direction to
# an actor and, to h3, an order for a still image. Cut, like every other word
# this pipeline cannot afford the model to hear.
_FREEZE_RX = re.compile(r"[^，,；;。]*(?:完全静止|一动不动|纹丝不动|无其他动作|没有其他动作|目光不动|面无表情)[^，,；;。]*[，,；;。]?")


def unfreeze(txt):
    return _FREEZE_RX.sub("", txt or "").strip("，,；; ")


def _performance_clause(shot, bp, face_only=False):
    txt = filmwords.soften_face(filmwords.strip_camera(unfreeze(_plain((shot or {}).get("performance")) or "")),
                                combat=is_combat(shot))
    if not txt:
        return ""
    txt = look.destrip(filmwords.substitute(txt), bp or {})
    parts = [p.strip() for p in re.split(r"[；;。]", txt) if p.strip()]
    if face_only:
        # the first frame: only what a face is doing, a still has no 过程 -
        # and of a change (眼睛从微眯到睁大) it gets the START, because frame
        # 0 is the beginning of the shot and the clip carries the change;
        # given both ends Flux drew the end (睁大 + 微皱 + 轻抿 = the pained
        # face of C01S06, 2026-10-14), and h3 held it
        parts = [re.sub(r"从(.{1,6}?)(?:到|变为|变成|转为|转成)(.{1,8}?)$", r"\1", q.strip())
                 for p in parts for q in re.split(r"[，,、]", p)
                 if q.strip() and _FACE_RX.search(q)][:3]
        return ("，".join(parts) + "。") if parts else ""
    out = ""
    for p in parts:
        if out and len(out) + len(p) > PERFORMANCE_MAX:
            break
        out += p + "；"
    return out.rstrip("；") + "。" if out else ""


# THE PROMPT AUDIT (2026-09-30: "以后prompt加一项校验，宁可过于详细，也比少
# 好"). Every rule in CLAUDE.md is a thing a prompt must carry or must not;
# until now each was enforced where its text was built, and a field nobody had
# filtered (the beats' camera talk) or a clause a code path forgot (the costume
# on a later segment) went straight to the model. This runs LAST, over the
# exact string h3 is about to get: what must be there and is not is ADDED from
# the script and the blueprint, what must not be there is cut, and both are
# logged. Erring long: a missing costume costs a man in a modern suit, an
# extra clause costs nothing.
PROMPT_AUDIT = os.environ.get("BOOKREEL_PROMPT_AUDIT", "1") not in ("0", "", "false")
_ERA_PREFIX_RX = re.compile(r"^[^，。；]*?(?:世纪[初中末]?|年间|时期|年代|国|朝)")
_LATIN_RX = re.compile(r"[A-Za-z][A-Za-z0-9.\-]*")


def _place_phrase(shot):
    """Where the shot is, as a plain noun phrase from its own scene prose -
    the era cut off the front (a date plus a place is a caption shape)."""
    t = filmwords.strip_camera(filmwords.strip_glow(_scene_text(shot) or ""))
    first = re.split(r"[，。；]", t)[0] if t else ""
    first = _ERA_PREFIX_RX.sub("", first).strip()
    return first[:24]


def audit_video_prompt(text, shot, bp, i=0):
    """(prompt, added, removed) - `prompt` is what goes to h3."""
    if not PROMPT_AUDIT or not shot:
        return text, [], []
    bp = bp or {}
    added, removed = [], []
    t = text
    for fn, label in ((filmwords.strip_camera, "镜头/画面用语"), (filmwords.strip_glow, "发光词")):
        t2 = fn(t)
        if t2 != t:
            removed.append(label)
            t = t2
    t2 = _denames(t, bp)
    if t2 != t:
        removed.append("人名")
        t = t2
    t2 = _LATIN_RX.sub("", t)
    if t2 != t:
        removed.append("拉丁字母")
        t = t2
    t2 = t.replace("：", "，").replace(":", "，")
    if t2 != t:
        removed.append("冒号")
        t = t2
    # what must be there
    head = []
    pc = filmwords.dedigit(_denames(_people_clause(shot, bp), bp))
    people = _plain((bp.get("world") or {}).get("people")) or ""
    # Compared the way `t` now reads: the colon of a fight's 场中央只有两个人
    # 在交手：… was turned into a comma three lines up, so the clause was
    # never found and went in a SECOND time on every fight shot.
    pc_cmp = pc.replace("：", "，").replace(":", "，")
    if pc and pc_cmp.split("，")[0] not in t and pc_cmp[:12] not in t:
        head.append(pc)
        added.append("人物（人数/年龄/性别/族裔/服装）")
    else:
        if people and people[:6] not in t:
            head.append(people + "。")
            added.append("族裔")
    # every named person's clothes, one by one - 段誉's were missing from the
    # people clause of every fight shot he stands in
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or [])}
    for cid in cast_ids(shot, bp):
        c = by_id.get(cid) or {}
        if is_group(c):
            continue
        cos = re.split(r"[，。;；,]", filmwords.strip_weapons(_plain(c.get("costume")) or ""))[0].strip()
        if cos and cos[:6] not in t and cos[:6] not in "".join(head):
            g = _plain(c.get("gender"))
            side = "在一旁观看的" if (fighters(shot, bp) and cid not in fighters(shot, bp)) else ""
            head.append("一名%s%s%s穿%s。" % (side, filmwords.dedigit(_age_words(c.get("age"))),
                                           "男子" if g.startswith("男") else "女子" if g.startswith("女") else "人",
                                           filmwords.dedigit(cos)))
            if "服装" not in added:
                added.append("服装")
    def _norm(x):
        return filmwords.dedigit(_denames(filmwords.strip_camera(unfreeze(x or "")), bp))
    beats = shot_beats(shot)
    if i == 0 and not any(_norm(b["do"])[:6] in t for b in beats):
        act = _norm(_plain(shot.get("action")) or "")
        act0 = re.split(r"[，。；]", act)[0] if act else ""
        if act0 and act0[:6] not in t:
            head.append(act0 + "。")
            added.append("动作")
    tail = []
    place = _place_phrase(shot)
    if place and place[:4] not in t:
        tail.append(("" if place.startswith(("在", "于")) else "在") + place + "。")
        added.append("地点")
    mouth = _mouth_clause(shot, False, bp)
    if "嘴唇" not in t and mouth:
        tail.append(mouth)
        added.append("嘴")
    facing = filmwords.dedigit(_denames(_facing_clause(shot, bp), bp))
    if facing and "目光" not in t and "面对面" not in t:
        tail.append(facing)
        added.append("说话的人朝向谁")
    if i == 0:
        face = filmwords.dedigit(_denames(_performance_clause(shot, bp, face_only=True), bp))
        if face and face[:6] not in t:
            tail.append(face)
            added.append("表情")
    en = _energy_clause(shot, i)
    if en and "出手迅猛" not in t:
        tail.append(filmwords.dedigit(en))
        added.append("打斗力度/攻防/内力")
    if is_combat(shot):
        t2 = filmwords.sword_paths(t)
        if t2 != t:
            added.append("剑招走向")
            t = t2
    if head or tail:
        t = "".join(head) + t + "".join(tail)
    return filmwords.dedigit(re.sub(r"[，]{2,}", "，", t)), added, removed


def _cast_names(bp):
    """Every name the cast answers to, longest first.

    "沈父（沈守正）" is two names, and both were painted into the picture.
    """
    out = set()
    for c in (bp.get("characters") or []):
        nm = str(c.get("name") or "").strip()
        if not nm:
            continue
        out.add(re.sub(r"[（(].*?[）)]", "", nm).strip())
        out.update(x.strip() for x in re.findall(r"[（(](.*?)[）)]", nm))
    return sorted((n for n in out if len(n) >= 2), key=len, reverse=True)


def _denames(text, bp):
    """A name is a caption waiting to happen: h3 wrote 沈墨 and 沈守正 across
    the frame in plain type. The composition field is where they mostly come
    from ("沈墨在左1/3线"), and the picture does not need them - who is in
    frame is settled by the first frame and by _people_clause."""
    for nm in _cast_names(bp):
        text = text.replace(nm, "人物")
    return re.sub(r"(人物)(\1)+", r"\1", text)


def _film_prompt(shot, bp, fmt):
    """Camera, then the scene, then who is in it - all as prose."""
    # NO era/place clause. Writing it as prose was not enough: C01S25 still
    # painted "崇祯六年江南苏州府及运河沿线" across the bottom of the frame,
    # because a date plus a place name IS a caption whatever the punctuation -
    # it is the shape of a documentary lower-third. What anchors the period
    # instead is the first frame (a verified plate) and the LLM's own scene
    # text, which opens "明代苏州汉人青年男子…" and has never been painted.
    head = ""
    # The same word substitutions the reference plates get: 油灯 became a
    # glass-chimney kerosene lamp in every night clip because this side could
    # not reach story_server's table (see services/filmwords.py), and "数字"
    # wrote Arabic numerals across Ming paper.
    body = look.destrip(filmwords.substitute(filmwords.soften_face(filmwords.strip_camera(filmwords.strip_glow(
        unfreeze(_scene_text(shot)))), combat=is_combat(shot))), bp)
    if is_combat(shot):
        body = filmwords.sword_paths(body)
    # The head-count goes BEFORE the scene, not after it. It used to sit at the
    # very end, behind 250 characters of description, which is the least
    # weighted position in the prompt - and h3 went on drawing its single
    # subject twice (沈墨 at one desk in C01S02, 阿蘅 side by side in C01S08).
    # Opening tokens set the picture, which is the same reason the camera block
    # is first and the reason a portrait prompt has to open with the portrait.
    # This is also subject-first in the ordinary sense: who is in frame is the
    # subject, and the scene is what is behind them.
    # The mouth clause sits with the people clause, not at the tail. The tail
    # is the least weighted position there is - that is what moved 画面中只有
    # 一个人 up here - and C01S14's scene prose opened with 嘴唇微张说出两个字
    # while the closing clause sat at character 151 of 164.
    # Performance after the scene: the scene sets the picture, the performance
    # says what the faces in it are doing. Effort on a fight goes with the
    # people, where it is weighted like the count.
    # bp reaches the mouth clause: without it _speaker_clause was "" on
    # segment 0 and the speaker was only ever named from segment 2 on.
    parts = [_camera_prefix(shot).strip(), head, _people_clause(shot, bp),
             _mouth_clause(shot, bp=bp), _facing_clause(shot, bp), _crowd_bg_clause(shot, bp),
             _energy_clause(shot), body, _performance_clause(shot, bp), _body_clause(shot)]
    # Digits last, over the WHOLE assembled prompt: they arrive from the camera
    # block, from the cast's age and from the model's own scene prose, and a
    # bare number is both a caption h3 will paint and a licence to put Arabic
    # numerals on Ming paper.
    return filmwords.dedigit(_fix_aspect(_denames(" ".join(p for p in parts if p), bp), fmt))


def _camera_prefix(shot):
    """The shot's own camera block, in words a video model acts on.

    The LLM writes shot_size / angle / height / movement / dof / composition
    per shot and none of it used to reach h3 - the prompt was the scene
    description alone, so the model chose its own framing every time and chose
    something similar every time. This goes at the FRONT: opening tokens set
    the picture, and framing is the first thing about a picture.
    """
    if not CAMERA_IN_PROMPT:
        return ""
    cam = shot.get("camera") or {}
    if not isinstance(cam, dict):
        return ""
    bits = [_SIZE_WORDS.get(str(cam.get("shot_size") or "").strip().upper()),
            _ANGLE_WORDS.get(_plain(cam.get("angle"))),
            _HEIGHT_WORDS.get(_plain(cam.get("height")))]
    mv = _plain(cam.get("movement"))
    move = _MOVE_WORDS.get(mv)
    if move and mv != "固定":
        sp = _SPEED_WORDS.get(_plain(cam.get("movement_speed")))
        if sp:
            move = move.replace("慢慢", sp).replace("平移", sp + "平移")
    bits.append(move)
    bits.append(_DOF_WORDS.get(_plain(cam.get("dof"))))
    # Only the half of the composition that names things in the room survives.
    comp = _plain(cam.get("composition"))
    if comp and len(comp) <= CAMERA_COMPOSITION_MAX and not _JARGON_RX.search(comp):
        bits.append(comp)
    bits = [b for b in bits if b]
    return "，".join(bits) + "。" if bits else ""


# Every one of xuemang's 108 video_prompts ended with 9:16竖幅 while the film
# renders 1088x608 landscape from 16:9 plates - the blueprint's visual_style
# said 9:16 and the aspect rode along into the shot text. So each frame was
# told "vertical" as it was drawn wide. Only a ratio followed by 竖/横 is
# touched: 明暗比5:1 and 4:1 are lighting contrast, not aspect, and 11 shots
# carry those in the same sentence.
_ASPECT_RE = re.compile(r"(?:9\s*:\s*16|16\s*:\s*9)\s*(?:竖幅|横幅|竖|横)")


def _fix_aspect(prompt, fmt):
    """Make the prompt's stated aspect agree with the canvas being rendered."""
    want = "16:9横幅" if (fmt or "16:9") == "16:9" else "9:16竖幅"
    return _ASPECT_RE.sub(want, prompt)


# h3 pins the first frame, so a later segment opens on a still and, given the
# same prompt as the segment before it, redraws the scene instead of carrying
# the action on. Measured over chapter 1: mean inter-frame motion 14.1 in
# segment 1 and 3.6 / 5.5 / 6.1 in the three that follow, in 7 of 8 clips.
# Naming the segment's place in the action is what gives it somewhere to go.
# Phrased as what the picture does, never as what it should stop doing - a
# negation puts the noun back in the frame (CLAUDE.md).
SEGMENT_CONTINUITY = os.environ.get("BOOKREEL_SEGMENT_CONTINUITY", "1") not in ("0", "", "false")


# A shot's action, cut into the beats it is actually made of. Written by the
# 分镜 (action_beats), because only the script knows that "叩门、门开、跨进去"
# is three things and not one said three ways.
_BEAT_SPLIT = re.compile(r"[，；,;]|然后|接着|随后|继而|旋即|复又")


def shot_beats(shot):
    """[{"do": str, "sfx": [name]}] for one shot, in order.

    The script has to have written them. Splitting the shot's own `action` on
    its commas was tried and is wrong: that sentence is a DESCRIPTION, and its
    clauses are simultaneous, not sequential. C01S02's action splits into
    "坐在石凳上 / 右手执笔书写 / 左手压着废纸 / 笔尖落纸", which are four things
    true at the same moment - handing segment 3 "左手压着半张写废的纸" as its
    action would lose the writing altogether. Only the 分镜 knows that 叩门、
    门开、跨进去 is three things and not one thing said three ways, so a script
    written before action_beats existed keeps the old whole-prompt behaviour
    until it is regenerated.
    """
    out = []
    for b in ((shot or {}).get("action_beats") or []):
        do = str((b.get("do") if isinstance(b, dict) else b) or "").strip()
        sfx = foley.normalize((b.get("sfx") or []) if isinstance(b, dict) else [])
        if do:
            out.append({"do": do, "sfx": sfx})
    return out


def _segment_prompt(prompt, i, nseg, shot=None, bp=None, fmt="16:9",
                    voice_secs=None, seg=None, voice_start=0.0):
    """The prompt for segment i of nseg, told what happens IN THIS SEGMENT.

    Two things were wrong with sending the shot's whole prompt to every
    segment. h3 pins the first frame, so a later segment opens on a still and,
    given the scene described all over again, redraws the scene instead of
    carrying the action on - measured over chapter 1, mean inter-frame motion
    14.1 in segment 1 against 3.6 / 5.5 / 6.1 in the three after it, in 7 of 8
    clips. And the action itself is in that text: told "他推开门" four times in
    a row, the model opens the door four times. A door that opens twice in one
    shot is not a continuity slip, it is the shot being unusable.

    So from segment 2 on, the scene description is NOT repeated. The picture
    is already there - it is the frame this segment starts from - and what
    goes in instead is this segment's own beat, the framing, and who is in
    frame. Where the script gives no beats, the old text still goes through:
    one beat is one action, and repeating it is only wrong when there is
    something else that should have happened.

    The camera half has to agree with the shot's own `movement`. It used to
    say "人物与镜头保持运动" on every segment of every shot, and 83 of this
    script's 115 shots are 固定(lock-off) - 59 of them long enough to be
    rendered in more than one segment. A locked frame that drifts is what a
    cut reads as wrong.

    Said as what the picture DOES, never as what it should stop doing - a
    negation puts the noun back in the frame (CLAUDE.md).
    """
    # Does THIS segment carry speech? _shot_audio lays the narrator first and
    # the dialog after it, back to back, so the sound occupies [0, voice_secs).
    # True = no speech anywhere in this segment, None = the line stops inside
    # it, False = talking throughout. Worked out before anything returns,
    # because SEGMENT 0 IS A SEGMENT: a 3.75 s shot carrying 0.74 s of dialog
    # (C01S07, C01S14) has no later segment to gate on, and three quarters of
    # it was a mouth moving against silence.
    quiet = False
    if voice_secs and seg:
        a, b2, end = i * float(seg), (i + 1) * float(seg), float(voice_secs)
        # Speech runs [voice_start, end): a cold start's lead is silence, and
        # counting it made C01S14's 0.46 s line look like 1.46 s of talking.
        spoken = max(0.0, min(end, b2) - max(a, float(voice_start or 0.0)))
        # Under about a third of the segment, the mouth is closed for the
        # whole of it. h3 cannot do "say two words, then stop": told 说完这句
        # 后嘴唇闭合, C01S14 (0.46 s of 别碰 in 3.75 s) was still mouthing at
        # 3.0 s. A closed mouth under a two-syllable whisper is a smaller lie
        # than three seconds of talking under silence.
        quiet = (True if spoken < 0.35 * float(seg)
                 else (None if end < b2 - 0.5 else False))
    if not SEGMENT_CONTINUITY or nseg <= 1 or i <= 0:
        shut = _mouth_clause(shot, quiet, bp) if shot else ""
        if not shut or shut in prompt:
            return prompt
        # The people clause as it reads in the FINISHED prompt - de-digited
        # (二十二岁, not 22岁) and de-named - or the search misses and the
        # clause lands on the tail, which is the position this exists to avoid.
        pc = filmwords.dedigit(_denames(_people_clause(shot, bp or {}), bp or {})) if shot else ""
        if pc and pc in prompt:
            return prompt.replace(pc, pc + shut, 1)     # beside the people, not at the tail
        return prompt + shut
    mv = str(((shot or {}).get("camera") or {}).get("movement") or "")
    locked = "固定" in mv or "lock" in mv.lower()
    # …unless the previz is pushing. A lock-off on a close size gets a slow
    # push (stage.LOCKOFF_PUSH), and telling the model the frame is nailed down
    # while the control video moves it is the same contradiction as the old bug
    # that asked for a moving camera on a locked-off shot, pointing the other
    # way. The text follows the previz, because the previz is what h3 sees.
    # Said as what the PICTURE does, in the words a viewer would use. This
    # header used to open every segment with 【承接】画面紧接首帧继续 … 落到
    # 这一镜的最后一个姿态 - a bracketed title plus stage directions, and h3
    # wrote "这一镜的最后一个" across the bottom of C01S12. It could only have
    # come from here: it is in no script and no scene, it is the renderer
    # talking to itself, and it sat in the most weighted position of the
    # prompt. The model has no channel for instructions; a line that is not a
    # thing to depict is a caption. So no brackets, and none of 镜/段/首帧/承接
    # - the continuation itself is not asked for in words at all, because the
    # first frame IS the mechanism that carries it.
    if locked and PREVIZ_ENABLED and stage.camera_moves(shot or {}):
        locked = False
        cam = "画面极缓慢地靠近，人物继续动作"
    else:
        cam = "人物继续动作" if locked else "人物在动，画面也跟着移动"
    beats = shot_beats(shot)
    if len(beats) > 1:
        # This segment's own beat, and nothing of the last one. The phase word
        # separates two segments that land on the same beat, so even then they
        # are asked for different moments rather than the same one twice.
        b = beats[min(len(beats) - 1, i * len(beats) // max(1, nseg))]
        phase = ""      # painted on C01S01 at 12.5 s; the beat says enough
        bp = bp or {}
        # The beat is the LLM's own prose and gets everything the scene text
        # gets: objects substituted, anachronisms cut. _denames runs over the
        # WHOLE assembled prompt at the end, not over the beat alone - the
        # names mostly arrive through camera.composition ("沈墨在左1/3线"), and
        # a segment prompt that skipped that step put the name straight back
        # into the picture as painted type.
        do = look.destrip(filmwords.substitute(filmwords.soften_face(filmwords.strip_camera(filmwords.strip_glow(
            unfreeze(b["do"]))), combat=is_combat(shot))), bp).rstrip("。")
        if is_combat(shot):
            # 刺 / 抹 / 点 / 撩 said as where the blade goes, or every one of
            # them is drawn as an overhead chop (2026-10-14)
            do = filmwords.sword_paths(do)
        # Effort yes, `performance` no: that column describes the whole shot
        # move by move (少年拍掌时…踉跄时左膝一软), and handed to every segment
        # it is the same action four times - the door that opens twice.
        keep = [p for p in (_camera_prefix(shot).strip(), _people_clause(shot, bp),
                            _mouth_clause(shot, quiet, bp), _facing_clause(shot, bp),
                            _crowd_bg_clause(shot, bp), _energy_clause(shot, i), _body_clause(shot)) if p]
        return filmwords.dedigit(_fix_aspect(_denames(
            ("%s。" % do) + " ".join(keep), bp), fmt))
    # No action_beats on this chapter, so there is no per-segment action to
    # send. Re-sending the WHOLE prompt makes the model redraw the scene
    # instead of carrying it on (mean inter-frame motion 14.1 in segment 1
    # against 3.6 after it); the 【承接】 header used to say "continue" and was
    # painted on screen for its trouble. What is left that is both drawable and
    # not the scene: who is in frame, what they are doing, the mouth and the
    # anatomy. The scene is already there - it is the frame this segment starts
    # from.
    act = look.destrip(filmwords.substitute(filmwords.strip_camera(str((shot or {}).get("action") or ""))), bp or {})
    keep = [p2 for p2 in (_people_clause(shot, bp or {}) if shot else "",
                          _mouth_clause(shot, quiet, bp) if shot else "",
                          _facing_clause(shot, bp) if shot else "",
                          _crowd_bg_clause(shot, bp) if shot else "",
                          _energy_clause(shot) if shot else "",
                          act.strip(),
                          _body_clause(shot) if shot else "") if p2]
    out = filmwords.dedigit(_fix_aspect(_denames(" ".join(keep), bp or {}), fmt)) if keep else prompt
    # A chapter with no action_beats sends the shot's whole prompt again, and
    # that prompt decided the mouth once, for the shot. This segment can still
    # be its silent tail.
    shut = _mouth_clause(shot, quiet, bp) if shot else ""
    if shut and shut not in out:
        out += shut
    return out


# ----------------------------------------------------------------------
# foley -- the sound a movement makes
# ----------------------------------------------------------------------
# A picture of a door opening with nothing on the soundtrack reads as a fault,
# not as a quiet moment, and every clip this pipeline has ever produced was
# either narration over silence or narration over nothing. The script has had
# somewhere to write this since the beginning (sound.foley, sound.sfx) and
# nothing ever read it; the mux even carried a comment promising "the script's
# room_tone/ambience/foley go over it in the sound pass" for a pass that did
# not exist.
#
# Three decisions worth keeping:
#
#   * The library is GLOBAL (sfx/ beside voices/), not per project. 开门 is the
#     same recording in every novel, and a per-project library would be
#     regenerated from scratch every time.
#   * A real recording WINS. sfx/<name>.wav is looked up before the generated
#     sfx/<name>.mp3, so dropping a proper foley pack in replaces what this box
#     made without renaming or regenerating anything.
#   * The sounds go on the CLIP, not on the film - the opposite of the music
#     beds, and for a concrete reason. A bed belongs to a run of shots, so it
#     is mixed once on the joined picture. Foley belongs to the picture it
#     comes from, and the 4K pass copies a clip's audio through untouched
#     (-c:a copy) while the chapter join stream-copies both. Put them on the
#     clip and they reach the 4K clips and the chapter film for free; put them
#     on the film and the 4K clips are silent.
#   * The library is keyed by the world's MATERIALS, not by project and not
#     by nothing. A Ming courtyard door is two heavy leaves turning on wooden
#     pivots with an iron ring pull and a threshold to step over; a 2026
#     apartment door is a hollow slab on steel hinges with a latch that
#     clicks. They sound nothing alike, so one flat folder would hand the
#     second novel the first one's doors. `look.profile()` names the shelf
#     (sfx/chinese_historical/, sfx/modern/, …) and two novels in the same
#     material world share it, which is the whole point of not keying by slug.
SFX_DIR = os.path.join(ROOT, "sfx")
SFX_ENABLED = os.environ.get("BOOKREEL_SFX", "1") not in ("0", "", "false")
# Under the narration, never over it. The per-sound gains in services/foley.py
# are relative to each other; this is the one knob for the layer as a whole.
SFX_GAIN = float(os.environ.get("BOOKREEL_SFX_GAIN", "0.55"))
# The crowd's murmur, relative to the vocabulary's own gain for it; the rest
# of its level comes from the shot (_place_loops) and from the voice ducking
# it (_mix_sfx). 1.0 is what tianlongbabu ch01 shipped with on 2026-10-06 and
# was heard as noise under every line.
MURMUR_LEVEL = float(os.environ.get("BOOKREEL_MURMUR_LEVEL", "1.0"))
# One name twice inside this window is one sound (beat + shot-level 入鞘).
SFX_DEDUP_SECONDS = float(os.environ.get("BOOKREEL_SFX_DEDUP", "0.6"))
# A one-shot longer than this is faded out at it.
SFX_ONE_MAX = float(os.environ.get("BOOKREEL_SFX_ONE_MAX", "1.6"))
# The ambience bed under the voice: sidechaincompress threshold (linear
# amplitude, 0.02 is about -34 dBFS) and ratio.
DUCK_THRESHOLD = float(os.environ.get("BOOKREEL_DUCK_THRESHOLD", "0.02"))
DUCK_RATIO = float(os.environ.get("BOOKREEL_DUCK_RATIO", "6"))


def sfx_dir(bp):
    return os.path.join(SFX_DIR, look.profile(bp))


# file stem -> make-fight-foley.py --only key. These are built, not generated.
# blade_clash and swish are NOT here: no synthesis sounded like metal (16
# re-rolls of the 当 build, 2026-09-30), so their takes are real recordings
# (Mixkit, see sfx/CREDITS.txt) and 重出 treats them as the recordings they are.
# The murmur, 喊好 and the hall's cheer stay built, because they are Mandarin
# and every crowd recording on Mixkit is English - crowd_cheer went to Mixkit
# for an afternoon and SenseVoice heard it as English ("The."), 2026-09-30.
SFX_SYNTH = {"crowd_murmur": "murmur", "shout": "shout", "crowd_cheer": "cheer", "remark": "remark"}


def _trash_takes(d, stem):
    """Move every .wav take of a sound (<stem>.wav, <stem>_2.wav …) to
    sfx/_trash/<stem>-<time>/ and return that folder."""
    dst = os.path.join(SFX_DIR, "_trash", "%s-%s" % (stem, time.strftime("%Y%m%d-%H%M%S")))
    os.makedirs(dst, exist_ok=True)
    for f in [os.path.join(d, stem + ".wav")] + glob.glob(os.path.join(d, glob.escape(stem) + "_[0-9]*.wav")):
        if os.path.isfile(f):
            shutil.move(f, os.path.join(dst, os.path.basename(f)))
    return dst


def sfx_file(name, bp):
    """The file for a canonical sound in this world, or "".

    A hand-supplied .wav beats the generated .mp3 - that is the whole upgrade
    path for this layer, and the only way to get a real recording in.
    """
    stem = foley.filename(name)
    if not stem:
        return ""
    for ext in (".wav", ".mp3"):
        p = os.path.join(sfx_dir(bp), stem + ext)
        if os.path.isfile(p):
            return p
    return ""


def sfx_files(name, bp):
    """Every take of a sound: <stem>.wav, <stem>_2.wav … when hand-supplied or
    built (scripts/make-fight-foley.py), else the one generated file. Eight
    swishes in four seconds from ONE file is a machine gun; rotating takes is
    what a foley editor does with a flurry."""
    stem = foley.filename(name)
    if not stem:
        return []
    d = sfx_dir(bp)
    wavs = sorted(glob.glob(os.path.join(d, glob.escape(stem) + "_[0-9]*.wav")),
                  key=lambda p: int(re.findall(r"_(\d+)\.wav$", p)[0]))
    first = sfx_file(name, bp)
    return ([first] if first else []) + [w for w in wavs if w != first]


def _has_crowd(shot, bp):
    """Onlookers in the shot: a group character, or five or more people."""
    ids = [str(i) for i in ((shot or {}).get("characters") or [])]
    by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or [])}
    return len(ids) >= 5 or any(is_group(by_id[i]) for i in ids if i in by_id)


def _scene_crowd(shot, shots, bp):
    """A crowd anywhere in this shot's SCENE - same location, same slug line
    before its bracketed note. A close-up of two blades in a hall of forty
    lists two people, and the forty do not fall silent because the camera
    moved in (C01S03, 2026-09-30): off screen is still in the room."""
    base = lambda sh: (str(sh.get("location_id") or ""),
                       re.split(r"[（(]", str(sh.get("slug_line") or ""), 1)[0].strip())
    me = base(shot)
    return bool(me[0] or me[1]) and any(
        base(o) == me and _has_crowd(o, bp) for o in (shots or []))


def _steel_at(t, beats, nseg, starts, seg, said):
    """Which contact sound, if any, the beat ON SCREEN at output second `t`
    allows: 刀击 where that beat lists 刀击, 击打 where it lists 击打, nothing
    where it lists neither. The grade finds a motion peak in EVERY segment
    (`blows`) and used to hand each one a blade clash whatever was happening
    there: tianlongbabu C01S08 (a palm strike, 破风 only) clanged on every
    segment, and C01S09's second beat - the loser propping his sword on the
    floor, the fight over - clanged again (2026-10-14: "fighting already
    finish, but sword click sound still playing"). A shot with no beats
    falls back to what the shot-level sound lists."""
    if beats:
        i = 0
        if starts:
            for k, s0 in enumerate(starts):
                if t >= s0 - 0.05:
                    i = k
        elif seg and seg > 0:
            i = int(t // seg)
        b = min(len(beats) - 1, i * len(beats) // max(1, nseg))
        names = set(beats[b]["sfx"])
    else:
        names = set(said)
    if "刀击" in names:
        return "刀击"
    if "击打" in names:
        return "击打"
    return ""


def sfx_plan(shot, nseg, seg, vdur, bp=None, hit=None, cheer=None, grade=None, scene=None):
    """(one_shots, loops) for one clip.

    one_shots: [(path, seconds_into_the_clip, gain)] - fired when the SEGMENT
    that shows that beat begins, which is the only timing information this
    pipeline actually has. It is not frame-accurate and does not need to be:
    the ear forgives a door heard a quarter-second early far more readily than
    a door that is never heard at all.

    loops: [(path, gain)] - run under the whole clip.

    hit: seconds into the clip where fightcut found the blow land. The clash of
    the beat on screen at that moment goes ON the hit instead of at its
    segment's start - a clang a second before the blades meet is the one
    mistake an ear does not forgive - and the air of the swing (破风) just
    before it.

    grade: what fightcut.apply returned. When it carries `hits` and `swings`
    the weapons are driven by the PICTURE, not by the script: steel on every
    hit, air a beat before it, air on every other swing (2026-09-30: one
    swish per clip at a segment boundary read as no swish at all). The
    script's own 破风/刀击 are then left out - fired at segment starts they
    were the clang a second early - and its other sounds land where their
    segment starts on screen.
    """
    ones, loops, seen = [], [], set()
    takes, fired = {}, {}

    def add(name, at):
        sp, paths = foley.spec(name), sfx_files(name, bp)
        if not sp or not paths:
            return
        if sp["kind"] == "loop":
            if paths[0] not in seen:
                seen.add(paths[0])
                loops.append((paths[0], sp["gain"] * SFX_GAIN, name))
            return
        at = max(0.0, min(at, max(0.0, vdur - 0.4)))
        # The same sound twice within half a second is a stutter, not two
        # sounds. The beat's 入鞘 fired at 0.20 and the shot-level 入鞘 at
        # 0.40 on C01S11/12 (2026-10-06), and every shot that listed a sound
        # both ways got it doubled: two sheathings, two cloth rustles, two
        # clangs 0.2 s apart. A real double hit comes from the picture (two
        # `hits`) and those are never that close.
        if any(abs(at - t) < SFX_DEDUP_SECONDS for t in fired.get(name, [])):
            return
        fired.setdefault(name, []).append(at)
        # takes rotate, and the rotation starts somewhere different on each
        # shot: counted from 0 every clip, every reaction shot opened on
        # remark.wav and every flurry on swish.wav
        k = takes.get(name, sum(map(ord, str(shot.get("id") or ""))))
        takes[name] = k + 1
        ones.append((paths[k % len(paths)], at, sp["gain"] * SFX_GAIN))

    grade = grade or {}
    g_hits = [h for h in (grade.get("hits") or ([hit] if hit is not None else [])) if h is not None]
    g_hits = sorted(g_hits + list(grade.get("blows") or []))
    # Only a FIGHT is driven by the grade. The calm grade (CALM_SPEED) returns
    # the same motion peaks, and every one of them got a swish: a man
    # sheathing his sword (C01S10/12), a close-up of a pale face (C01S11), an
    # elder glaring (C01S17) - blade sounds on through the scene after the
    # duel (2026-10-14, the other half of "sword sound still playing").
    if not is_combat(shot):
        g_hits, grade = [], {}
    driven = bool(g_hits or grade.get("swings"))
    snd = (shot.get("sound") or {}) if isinstance(shot.get("sound"), dict) else {}
    beats = shot_beats(shot)
    said = [nm for b in beats for nm in b["sfx"]] + foley.normalize(
        list(snd.get("foley") or []) + list(snd.get("sfx") or []))
    # a fist fight lands flesh, a sword fight lands steel
    strike = "击打" if ("击打" in said and "刀击" not in said) else "刀击"
    skip = {"破风", "刀击", "击打"} if driven else set()
    starts = grade.get("seg_starts") or []

    if beats:
        last = -1
        for i in range(max(1, nseg)):
            b = min(len(beats) - 1, i * len(beats) // max(1, nseg))
            if b != last:
                # A touch after the segment's first frame: a transient exactly
                # on a cut reads as a click in the edit rather than as a sound
                # in the room.
                t0 = starts[i] if i < len(starts) else i * seg
                on_hit = hit is not None and i * seg <= hit < (i + 1) * seg
                for nm in beats[b]["sfx"]:
                    if nm in skip:
                        continue
                    add(nm, hit if (on_hit and nm in _COMBAT_SFX) else t0 + 0.20)
                last = b
    if driven:
        for h in g_hits:
            add("破风", h - 0.15)
            # steel only where the beat on screen has steel in it (_steel_at)
            st = _steel_at(h, beats, nseg, starts, seg, said)
            if st:
                add(st, h)
        for w in grade.get("swings") or []:
            add("破风", w - 0.06)
    elif hit is not None:
        add("破风", max(0.0, hit - 0.18))
    for nm in foley.normalize(snd.get("ambience")):
        add(nm, 0.20)
    # Shot-level foley has no beat to hang on, so a one-shot among it lands
    # early and a texture runs throughout.
    for nm in foley.normalize(list(snd.get("foley") or []) + list(snd.get("sfx") or [])):
        if nm in skip:
            continue
        add(nm, hit if (hit is not None and nm in _COMBAT_SFX) else 0.40)
    # People watching make noise. Under any shot with a crowd in it, a murmur;
    # on a watched fight, a cheer as the big blow lands (the ramp - the moment
    # the grade already slowed down for), else at the last hit.
    if _has_crowd(shot, bp) or _scene_crowd(shot, scene, bp):
        add("议论", 0.0)
        if is_combat(shot):
            at = cheer if cheer is not None else hit
            if at is not None:
                add("叫好", at + 0.35)
            # and single voices you can make out - 好！ 妙招！ - on up to two
            # other blows, never on top of the hall's cheer or of each other
            # (2026-09-30: "add voice 好！, 妙招！"). Not on every blow: a
            # shout on every clash is a sports broadcast.
            calls = [at] if at is not None else []
            for h in g_hits:
                if len(calls) >= (3 if at is not None else 2):
                    break
                if all(abs(h - c) > 0.9 for c in calls) and h + 0.3 < vdur - 0.4:
                    calls.append(h)
                    add("喊好", h + 0.30)
        # …and a remark you can make out (低语): one onlooker muttering a
        # short period line to his neighbour. The murmur bed is many voices
        # with the consonants filtered off on purpose, so nothing in it is a
        # sentence; without this the hall only ever shouts 好 (2026-10-14:
        # "观众只会说好功夫…众人的议论没有"). Never over a line of dialog,
        # never on top of a cheer or a blow, one on a fight and up to three
        # on a quiet reaction shot.
        if not spoken(shot) and vdur > 1.6:
            taken = [t for nm in ("叫好", "喊好") for t in fired.get(nm, [])]
            slots = ([0.25, 1.0, 1.7, 2.6] if is_combat(shot) else [0.5, 3.4, 6.5])
            got = 0
            for t in slots:
                if t + 1.0 > vdur - 0.3 or got >= (1 if is_combat(shot) else 3):
                    break
                if all(abs(t - c) > 0.6 for c in taken) and all(abs(t - h) > 0.45 for h in g_hits):
                    add("低语", t)
                    taken.append(t)
                    got += 1
    return ones, _place_loops(loops, shot, scene, combat=is_combat(shot))


# What is heard from OUTSIDE the walls. tianlongbabu's hall (2026-09-30:
# "ambient sounds doesn't sound right") had 风声 under every shot as the full
# open-air wind recording - a gale blowing through a closed 练武厅. Indoors
# those textures come through the walls and the paper windows: low-passed,
# and well under the room's own sound.
_OUTSIDE_LOOPS = {"风声", "雨声", "流水", "虫鸣", "鸟鸣", "马蹄", "木轮", "摇橹", "市集"}
# A walking loop under a duel is somebody strolling through the fight; the
# fighters' feet are the swishes and hits the picture drives.
_WALK_LOOPS = {"脚步", "跑步", "上楼"}
SFX_LOOP_FADE = 0.04


def _scene_offset(shot, scene):
    """Seconds of this shot's scene that play before it - so a texture carries
    on across the cut instead of starting from the top of the same file every
    shot (the same gust of wind at every cut is a loop you can hear)."""
    if not scene:
        return 0.0
    base = lambda sh: (str(sh.get("location_id") or ""),
                       re.split(r"[（(]", str(sh.get("slug_line") or ""), 1)[0].strip())
    me, t = base(shot), 0.0
    for o in scene:
        if o is shot or str(o.get("id")) == str(shot.get("id")):
            return t
        t = t + float(o.get("seconds") or 0) if base(o) == me else 0.0
    return 0.0


def _place_loops(loops, shot, scene, combat=False):
    """(path, gain, filter, start) for each texture: muffled when it is the
    outside heard from inside, dropped when it is a stroll under a fight,
    the murmur hushed while the hall watches a duel, and started where the
    scene's previous shot left the same texture."""
    inside = stage.shot_interior(shot)
    start = _scene_offset(shot, scene)
    tight = str(((shot or {}).get("camera") or {}).get("shot_size") or "").upper() in (
        "MCU", "CU", "ECU", "BCU")
    out = []
    for path, gain, name in loops:
        filt = ""
        if combat and name in _WALK_LOOPS:
            continue
        if inside and name in _OUTSIDE_LOOPS:
            filt, gain = "lowpass=f=650,lowpass=f=650,", gain * 0.45
        if name == "议论":
            # The hall's murmur ran at one level under all 24 shots of
            # tianlongbabu ch01 (2026-10-06: "background noise are annoying"):
            # under a duel, under a line, under a close-up of one face. It is
            # off-screen sound: quieter to start with (MURMUR_LEVEL), further
            # away when the camera is on a face, and behind the walls of the
            # voice when somebody speaks - plus the ducking in _mix_sfx.
            # …but not INAUDIBLE. Measured 2026-10-14 on the clips of
            # 2026-10-10: 0.6 x 0.6 x 0.6 on a 0.40 x 0.55 entry put the bed
            # at -53 dB RMS under C01S15's pauses and -49 dB under the duel
            # (the clashes are -24): "众人的议论没有". MURMUR_LEVEL is 1.0
            # now and each step is a few dB, with the sidechain duck doing
            # the work under a line.
            gain *= MURMUR_LEVEL
            if combat:
                gain *= 0.8
            if tight:
                gain *= 0.8
            if spoken(shot):
                gain *= 0.8
                filt += "lowpass=f=1400,"
        dur = _duration(path) or 0.0
        out.append((path, gain, filt, (start % dur) if dur > 1.0 else 0.0))
    return out


def _mix_sfx(src, dst, ones, loops, vdur):
    """Lay the foley onto a finished clip. Picture is stream-copied.

    A SECOND pass, deliberately, rather than another branch inside the mux
    above. That mux has three branches and every one of them carries the
    apad=whole_dur length that a bare apad cost two hours of GPU to learn;
    threading a variable number of extra inputs through all three is how that
    gets undone by accident. One more ffmpeg call on the finished file is
    cheap - it is an audio encode with -c:v copy - and it cannot reach the
    part that was hard to get right.
    """
    args, filt, labels, beds = ["-i", src], [], [], []
    for path, at, gain in ones:
        i = len(labels) + len(beds) + 1
        args += ["-i", path]
        lab = "s%d" % i
        # A one-shot is an EVENT, so it ends. A generated take that found no
        # event was cut at the extractor's 2.5 s ceiling, and a recording
        # keeps its whole ring-out; under an ECU of a sword going home two of
        # those at once were -17 dB of hiss for the whole shot (C01S12,
        # 2026-10-06). Anything past SFX_ONE_MAX fades out.
        keep = max(0.3, vdur - at)
        tail = ""
        if (_duration(path) or 0.0) > SFX_ONE_MAX:
            keep = min(keep, SFX_ONE_MAX)
            tail = "afade=t=out:st=%.3f:d=0.25," % max(0.0, keep - 0.25)
        filt.append("[%d:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    "atrim=0:%.3f,asetpts=N/SR/TB,%svolume=%.3f,adelay=%d|%d[%s]"
                    % (i, keep, tail, gain, int(at * 1000), int(at * 1000), lab))
        labels.append(lab)
    for lp in loops:
        path, gain = lp[0], lp[1]
        flt = lp[2] if len(lp) > 2 else ""
        at = lp[3] if len(lp) > 3 else 0.0
        i = len(labels) + len(beds) + 1
        # A texture is allowed to loop; a bed is not. The difference is that
        # rain has no tune to recognise, which is the whole reason the music
        # side had to stop doing this.
        args += ["-stream_loop", "-1", "-i", path]
        lab = "b%d" % i
        # Only a declick at the clip's edges. It was a 0.5 s fade each side,
        # and the chapter join is a stream copy, so the room's sound dipped
        # to nothing and swelled back at EVERY cut - an ambience that breathes
        # with the edit. A soft transition gets its fade from _film_dips.
        fade = min(SFX_LOOP_FADE, vdur / 6.0)
        filt.append("[%d:a]aformat=sample_rates=48000:channel_layouts=stereo,%s"
                    "atrim=%.3f:%.3f,asetpts=N/SR/TB,"
                    "afade=t=in:st=0:d=%.2f,afade=t=out:st=%.3f:d=%.2f,volume=%.3f[%s]"
                    % (i, flt, at, at + vdur, fade, max(0.0, vdur - fade), fade, gain, lab))
        beds.append(lab)
    if not labels and not beds:
        return False
    filt.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[base][key]")
    if beds:
        # The textures are summed into one bed and DUCKED under the clip's own
        # track - the voice. A murmur that is right under a line at a fixed
        # level is what reads as "noise"; one that steps back when somebody
        # speaks and comes back in the pause is a room. The key is the voice
        # track alone (the foley is not in it yet), and a silent shot's key
        # is digital silence, so nothing moves there.
        if len(beds) > 1:
            filt.append("%samix=inputs=%d:normalize=0:duration=first[bed]"
                        % ("".join("[%s]" % b for b in beds), len(beds)))
        else:
            filt.append("[%s]anull[bed]" % beds[0])
        filt.append("[bed][key]sidechaincompress=threshold=%.3f:ratio=%.1f:attack=15:"
                    "release=450:makeup=1:level_sc=1:mix=1[bedd]" % (DUCK_THRESHOLD, DUCK_RATIO))
        labels.append("bedd")
    else:
        filt.append("[key]anullsink")
    # a flurry is a dozen transients summed at full weight: limit, never clip.
    # And pad to the picture: amix ends a little before its first input does
    # (audio 4.48 s under a 4.54 s picture, 2026-09-30), and a clip whose
    # sound stops short of its picture is a hole at the chapter's stream-copy
    # join. Same fix as the mux's apad=whole_dur; -shortest still ends it.
    filt.append("[base]%samix=inputs=%d:normalize=0:duration=first,"
                "alimiter=limit=0.89:attack=2:release=60:level=disabled,"
                "apad=whole_dur=%.3f[aout]"
                % ("".join("[%s]" % l for l in labels), len(labels) + 1, vdur))
    _ff(args + ["-filter_complex", ";".join(filt), "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                "-ar", "48000", "-ac", "2", "-shortest", dst], timeout=900)
    return True


def _instruct_for(d):
    """A CosyVoice2 instruct line from one dialog entry's delivery block.

    The script writes eight fields per line -- emotion, pace, volume, pitch,
    timbre, emphasis, pause, breath -- and every one of xuemang's 40 lines has
    all eight. instruct2 wants a short spoken direction, not a spec sheet, so
    this takes the four that change how a line sounds most and the emphasis,
    and leaves pause/breath to the text's own punctuation. Handing it all eight
    made the direction longer than the line being said.
    """
    if not isinstance(d, dict):
        return ""
    if d.get("inner"):
        # a thought, not a line: close, quiet, unhurried
        return "请用很轻、很近、像在心里对自己说话的语气说"
    if not DIALOG_INSTRUCT:
        return ""
    dl = d.get("delivery") or {}
    if not isinstance(dl, dict):
        return ""
    bits = []
    emo = str(dl.get("emotion") or "").strip()
    if emo:
        bits.append("用%s的语气" % emo.rstrip("。，"))
    for label, field in (("语速", "pace"), ("音量", "volume"), ("音色", "timbre")):
        v = str(dl.get(field) or "").strip().rstrip("。，")
        if v and v != "无":
            bits.append("%s%s" % (label, v))
    if not bits:
        return ""
    out = "，".join(bits) + "说"
    emph = str(dl.get("emphasis") or "").strip()
    if emph and emph != "无":
        # The script writes emphasis as "word（why）"; the model only needs the
        # word, and the parenthetical is longer than the line as often as not.
        word = re.split(r"[（(]", emph, 1)[0].strip().rstrip("。，")
        if word:
            out += "，重读“%s”" % word
    return out[:180]


# Chinese subtitles stop being readable past about this many characters on a
# line; asr_server splits on the same number for the same reason.
SUB_MAX_CHARS = int(os.environ.get("BOOKREEL_SUB_MAX_CHARS", "18"))
_CUE_SPLIT = re.compile(r"(?<=[。！？!?；;，、,：:])")


# How far a cue boundary may be moved to land it in a real pause. Wide enough
# to cover what the character-count estimate gets wrong (measured worst case
# 0.77 s over the takes on disk), narrow enough that a boundary cannot jump to
# the wrong gap - CosyVoice's phrase pauses on these lines sit 1.2-2.4 s apart.
CUE_SNAP_WINDOW = float(os.environ.get("BOOKREEL_CUE_SNAP", "0.9"))


def _snap_cues(bounds, start, end, pauses):
    """Move each boundary between cues onto the nearest silence in the wav.

    `bounds` is the character-count estimate, one time per cue, last one at
    `end`. Character count says roughly where a line divides; the wav says
    exactly. A boundary is placed at the FAR edge of the gap it lands in, so
    the outgoing subtitle holds until the speech actually stops and the next
    appears as the next phrase begins - which is what "the caption matches the
    voice" means when you watch it.

    Returns None to mean "keep the estimate": no pauses, a single cue, or a
    span too short to seat every boundary in order. Never returns a boundary
    that sits on or before the one before it - a backwards cue is a negative
    duration, and _clamp_cues would silently drop the line rather than show it.
    """
    if not pauses or len(bounds) < 2:
        return None
    out, prev = [], start
    for b in bounds[:-1]:
        best, dist = None, CUE_SNAP_WINDOW
        for a, z in pauses:
            if z <= prev + 1e-6 or a >= end - 1e-6:
                continue
            mid = (a + z) / 2.0
            if abs(mid - b) < dist:
                best, dist = (a, z), abs(mid - b)
        t = best[1] if best else b
        if not prev < t < end:
            t = b                       # the gap does not fit here; estimate
        if not prev < t < end:
            return None                 # no room at all - use the estimate
        out.append(t)
        prev = t
    return out


def _split_cue(text, start, end, pauses=None):
    """One spoken line as one or more readable cues.

    Split points come from the line's own punctuation; the time comes from
    character count and is then snapped onto the pauses the model actually
    left (`_snap_cues`). Unlike asking the aligner this is deterministic and
    cannot fail in the middle of a ninety-minute render.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= SUB_MAX_CHARS:
        return [{"start": start, "end": end, "text": text}]
    parts, buf = [], ""
    for piece in _CUE_SPLIT.split(text):
        if not piece:
            continue
        if buf and len(buf) + len(piece) > SUB_MAX_CHARS:
            parts.append(buf)
            buf = piece
        else:
            buf += piece
    if buf:
        parts.append(buf)
    if len(parts) <= 1:
        return [{"start": start, "end": end, "text": text}]
    total = float(sum(len(p) for p in parts)) or 1.0
    span = end - start
    bounds, t = [], start
    for p in parts:
        t += span * (len(p) / total)
        bounds.append(t)
    snapped = _snap_cues(bounds, start, end, pauses)
    if snapped:
        bounds = snapped + [end]
    out, t = [], start
    for p, b in zip(parts, bounds):
        out.append({"start": round(t, 3), "end": round(b, 3), "text": p.strip()})
        t = b
    if out:
        out[-1]["end"] = round(end, 3)
    return out


def _clamp_cues(cues, secs):
    """A subtitle may only describe audio that is in the file.

    The mux is -shortest: if the voice track is longer than the picture, the
    tail of the line is not in the clip. A cue for it is then a subtitle for
    something nobody hears, and once the chapter is concatenated it sits on top
    of the next shot's own subtitles - C01S04's ran 12.5 s past its clip.
    Cut them where the sound stops. Applied when the take is written AND when
    the SRT is built, so clips that went out before this repair themselves.
    """
    out = []
    for c in cues or []:
        try:
            st, en = float(c["start"]), min(float(c["end"]), float(secs))
        except (KeyError, TypeError, ValueError):
            continue
        if en - st < 0.15:
            continue
        out.append({"start": round(st, 3), "end": round(en, 3),
                    "text": c.get("text") or ""})
    return out



# ----------------------------------------------------------------------
# the voice library -- a person's own recording, cut into a voice
# ----------------------------------------------------------------------
# A voice is a clip plus its EXACT transcript (voices/README.txt), and only
# four genuine speakers ever existed here; everything else was those four
# pitch-shifted. So: upload any recording, pick 3-10 s of clean speech (the
# speech runs are proposed from silencedetect), let SenseVoice transcribe it,
# correct the text, save. It is then a voice like any other - casting sees it
# at once, no restart.
VOICE_SRC = os.path.join(ROOT, "voices", "_sources")
_VOICE_NAME_RX = re.compile(r"^[A-Za-z0-9_\-\u4e00-\u9fff]{1,40}$")


def _voice_src(vid):
    vid = re.sub(r"[^a-f0-9]", "", str(vid or ""))[:16]
    p = os.path.join(VOICE_SRC, vid + ".wav")
    return p if vid and os.path.isfile(p) else None


def _speech_runs(path, floor_db=-35, gap=0.35):
    """[(start, end)] of speech in a recording, from silencedetect."""
    exe = _ffmpeg()
    pr = subprocess.run([exe, "-hide_banner", "-i", path, "-af",
                         "silencedetect=noise=%ddB:d=%.2f" % (floor_db, gap), "-f", "null", "-"],
                        capture_output=True, timeout=600)
    err = pr.stderr.decode("utf-8", "replace")
    dur = _duration(path)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    runs, t = [], 0.0
    for a in starts:
        if a - t > 0.25:
            runs.append((round(t, 2), round(a, 2)))
        nxt = [e for e in ends if e > a]
        t = nxt[0] if nxt else dur
    if dur - t > 0.25:
        runs.append((round(t, 2), round(dur, 2)))
    return runs, dur


def _voice_segments(runs, lo=3.0, hi=10.0):
    """Join speech runs across short pauses into 3-10 s candidate voices."""
    out, cur = [], None
    for a, b in runs:
        if cur and b - cur[0] <= hi and a - cur[1] <= 0.8:
            cur = (cur[0], b)
        else:
            if cur and cur[1] - cur[0] >= lo:
                out.append(cur)
            cur = (a, b)
    if cur and cur[1] - cur[0] >= lo:
        out.append(cur)
    return [{"start": round(max(0.0, a - 0.05), 2), "end": round(b + 0.1, 2)} for a, b in out if b - a <= hi + 0.5][:40]


def _voice_cut(src, start, end, dst, norm=True):
    start, end = float(start), float(end)
    if not 0 <= start < end or end - start > 30:
        raise ValueError("片段应在 0-30 秒之间")
    af = "loudnorm=I=-20:TP=-2:LRA=11," if norm else ""
    _ff(["-ss", "%.3f" % start, "-to", "%.3f" % end, "-i", src,
         "-af", af + "afade=t=in:d=0.02,areverse,afade=t=in:d=0.05,areverse",
         "-ar", "24000", "-ac", "1", "-c:a", "pcm_f32le", dst], timeout=120)


BUILTIN_WORK = "系统自带"


def _voice_work(name):
    """Which work (作品) a voice comes from: voices/<name>.json {"work": ...}.
    The built-in references (CosyVoice's demos and their shifted copies) are
    系统自带; anything else without a record is "" (未归类)."""
    meta = _read_json(os.path.join(ROOT, "voices", name + ".json"), {}) or {}
    w = str(meta.get("work") or "").strip()
    if w:
        return w
    if re.match(r"^(?:f_|m_|narrator_|st_)|^storyteller$", name):
        return BUILTIN_WORK
    return ""


def _set_voice_work(name, work):
    p = os.path.join(ROOT, "voices", name + ".json")
    meta = _read_json(p, {}) or {}
    work = " ".join(str(work or "").split())[:40]
    if work:
        meta["work"] = work
    else:
        meta.pop("work", None)
    if meta:
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        os.replace(p + ".tmp", p)
    elif os.path.isfile(p):
        os.unlink(p)


def _voice_version(name):
    """When this voice last changed: newest of its .wav/.txt/.rhythm and, for a
    .rhythm voice, the voice it borrows delivery from. A voice saved again
    under the same name is a different voice, and every cache keyed by the
    NAME alone (the audition mp3, the browser's copy of it) must notice."""
    d = os.path.join(ROOT, "voices")
    files = [os.path.join(d, name + ext) for ext in (".wav", ".txt", ".rhythm")]
    try:
        src = open(files[2], encoding="utf-8").read().strip()
        if src and _VOICE_NAME_RX.match(src):
            files += [os.path.join(d, src + ".wav"), os.path.join(d, src + ".txt")]
    except OSError:
        pass
    return int(max([os.path.getmtime(f) for f in files if os.path.isfile(f)] or [0]))


def character_line(slug, cid):
    """One real line this character says, to audition their voice on.

    From the 分镜 first - dialog carries the speaker's id, so it is exactly
    theirs, and its delivery is what the render will ask for - then from the
    book's own prose (a quote right after their name or an alias). Prefers a
    line of 12-45 characters: long enough to hear a voice, short enough to
    synthesize while the person waits. Returns {text, source, delivery} or {}."""
    base = os.path.join(PROJECTS, slug)
    cands = []
    sdir = os.path.join(base, "script")
    for fn in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
        if not re.match(r"^\d\d\.json$", fn):
            continue
        for sh in (_read_json(os.path.join(sdir, fn), {}) or {}).get("shots") or []:
            for d in sh.get("dialog") or []:
                line = str(d.get("line") or "").strip()
                if d.get("who") == cid and line:
                    cands.append({"text": line, "source": "分镜 %s" % sh.get("id"), "delivery": d})
    if not cands:
        bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
        c = next((x for x in bp.get("characters") or [] if x.get("id") == cid), {})
        names = [n for n in [c.get("name")] + list(c.get("aliases") or []) if n]
        names = [re.sub(r"[（(].*", "", n).strip() for n in names if n]
        cdir = os.path.join(base, "chapters")
        for fn in sorted(os.listdir(cdir)) if os.path.isdir(cdir) and names else []:
            prose = str((_read_json(os.path.join(cdir, fn), {}) or {}).get("prose") or "")
            for n in names:
                for m in re.finditer(re.escape(n) + r"[^“”。！？\n]{0,14}[道说问叫喝笑喊答][：:]?\s*“([^”]{2,80})”", prose):
                    cands.append({"text": m.group(1).strip(), "source": "原文 %s" % fn[:2], "delivery": {}})
    if not cands:
        return {}
    good = [x for x in cands if 12 <= len(x["text"]) <= 45]
    pick = max(good, key=lambda x: len(x["text"])) if good else max(cands, key=lambda x: len(x["text"]))
    text = pick["text"]
    if len(text) > 60:                      # keep the audition short: cut at a sentence end
        cut = max(text.rfind(ch, 0, 60) for ch in "。！？…")
        text = text[:cut + 1] if cut > 10 else text[:60]
    return dict(pick, text=text)


def _voice_pairs():
    """Every usable voice in voices/: a .wav with its .txt transcript beside it.

    CosyVoice2 is zero-shot with no speaker table, so this list IS the cast of
    available voices - there is nothing else to choose from.
    """
    d = os.path.join(ROOT, "voices")
    out = []
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.lower().endswith(".wav"):
            continue
        name = fn[:-4]
        if name.startswith("_"):            # scratch pairs from try-voice.sh
            continue
        if os.path.isfile(os.path.join(d, name + ".txt")):
            out.append(name)
    return out


# What every voice says in its audition clip. Short on purpose: this is
# synthesized on demand on the CPU, and a twenty-character line is the
# difference between a four-second wait and half a minute. It is a line of the
# film's own register rather than "test test", because a voice that suits a
# Ming courtroom can still sound wrong reading a test phrase.
VOICE_SAMPLE_TEXT = os.environ.get(
    "BOOKREEL_VOICE_SAMPLE", "崇祯六年秋，苏州城厢，日头偏了。")


def casting_path(slug):
    return os.path.join(PROJECTS, slug, "assets", "casting.json")


def _casting(slug):
    """Who speaks with which voice, for this project.

    {"narrator": "<voice>", "characters": {"ch01": "<voice>", ...}}

    Kept beside prompts.json because casting belongs with the reference art:
    the page that decides what a character LOOKS like is the one that should
    decide what they SOUND like. Missing or unreadable means "nothing cast",
    which falls back to the old behaviour rather than failing.
    """
    c = _read_json(casting_path(slug), {}) or {}
    chars = c.get("characters")
    return {"narrator": str(c.get("narrator") or "").strip(),
            "characters": chars if isinstance(chars, dict) else {}}


def _voice_for(who, cast, have):
    """The voice a speaker uses. `who` empty means the narrator.

    Order: what this project cast for them -> a voices/<id>.wav recorded under
    their own id (the old convention, still honoured) -> the narrator. A cast
    name that no longer has a .wav+.txt pair is ignored rather than passed to
    the tts service, which would 404 the whole shot's audio.
    """
    if who:
        pick = str((cast.get("characters") or {}).get(who) or "").strip()
        if pick and pick in have:
            return pick
        if who in have:
            return who
    nar = str(cast.get("narrator") or "").strip()
    return nar if nar in have else VOICE


def _speech_lines(shot, cast=None):
    """Everything heard in this shot, in order: the narrator, then the dialog.

    CosyVoice2 is zero-shot and has no speaker table, so a character speaks in
    their own voice only once one is cast for them (casting.json, set on the
    reference-image page) or somebody records voices/<id>.wav; otherwise the
    narrator reads their line too. That is a casting decision, not a failure,
    so it is not an error - but /api/video/plan reports which ids are missing.
    """
    cast = cast if isinstance(cast, dict) else {"narrator": "", "characters": {}}
    have = set(_voice_pairs())
    out = []
    nar = (shot.get("narration") or "").strip()
    if nar:
        out.append((_voice_for("", cast, have), nar, NARRATION_INSTRUCT))
    for d in shot.get("dialog") or []:
        line = (d.get("line") or "").strip()
        if not line:
            continue
        who = (d.get("who") or "").strip()
        out.append((_voice_for(who, cast, have), line, _instruct_for(d)))
    return out


def _asr_text(wav):
    """What SenseVoice hears in a wav (the ASR service), for the shot review."""
    boundary = "----bookreel%08x" % random.getrandbits(32)
    with open(wav, "rb") as f:
        data = f.read()
    mp = (("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"line.wav\"\r\n"
           "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + data +
          ("\r\n--%s--\r\n" % boundary).encode())
    r = urllib.request.Request("http://127.0.0.1:9101/transcribe", data=mp,
                               headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return (json.load(resp) or {}).get("text", "")


def _tts_up():
    """True when the tts service answers /health with status ok."""
    try:
        with urllib.request.urlopen(TTS + "/health", timeout=10) as r:
            return (json.load(r) or {}).get("status") == "ok"
    except Exception:                                       # noqa: BLE001
        return False


def _synth(text, voice, instruct="", timeout=1800):
    body = {"text": text, "voice": voice, "speed": 0.95, "format": "wav"}
    # instruct2 replaces the reference transcript with the direction, so send
    # the key only when there is one to send; an empty string would select the
    # instruct path with nothing in it.
    if instruct:
        body["instruct"] = instruct
    req = urllib.request.Request(
        TTS + "/tts", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _shot_order(key):
    """Sort key for "N:CxxSyy" - chapter, then shot number."""
    m = re.match(r"^(\d+):C(\d+)S(\d+)$", key or "")
    return (int(m.group(1)), int(m.group(3))) if m else (10 ** 6, 0)


def _adjacent(prev_key, key):
    """True when key is the very next shot after prev_key in the same chapter.

    Continuity may only be inherited from the shot immediately before: the
    queue can be re-ordered, re-run piecemeal, or resumed, and a tail frame
    from six shots ago is not continuity, it is a mistake.
    """
    m1 = re.match(r"^(\d+):C(\d+)S(\d+)$", prev_key or "")
    m2 = re.match(r"^(\d+):C(\d+)S(\d+)$", key or "")
    if not m1 or not m2:
        return False
    if m1.group(1) != m2.group(1) or m1.group(2) != m2.group(2):
        return False
    return int(m2.group(3)) == int(m1.group(3)) + 1


_SOFT_CUT = ("淡", "叠", "化", "黑场", "白场")


# Roughly what fraction of a wide frame each shot size occupies. Only the
# ratios between neighbours matter, and only to decide how far to punch in.
_SHOT_SCALE = {"ELS": 1.0, "LS": 0.85, "MLS": 0.70, "MS": 0.55,
               "MCU": 0.42, "CU": 0.30, "ECU": 0.18, "BCU": 0.18}


def _inherit_ratio(prev, cur):
    """How to start `cur` from `prev`'s last frame, or None to use a plate.

    Returns 1.0 to inherit the frame as it is, or a fraction to centre-crop it
    first. h3 pins the first frame exactly, so this is the whole question of
    whether two shots are one continuous piece of film or two separate ones:

    * different place or hour, or a dissolve rather than a cut  -> a new scene,
      no inheritance. 13 of this script's 107 joins.
    * a different camera angle -> the geometry changes and a crop cannot fake
      it.
    * a pull-back (CU then MS) -> the wider frame needs pixels outside the one
      we have. Nothing to inherit; 17 joins fall back to the plate here.
    * the same framing -> inherit outright. 6 joins.
    * a punch-in (MS then CU) -> crop the tail frame down to the tighter
      framing and let h3 redraw the detail. 21 joins, and this is the case that
      makes a scene read as one continuous take rather than as unrelated
      clips.
    """
    if not prev or not cur:
        return None
    if (prev.get("location_id") or "") != (cur.get("location_id") or ""):
        return None
    if (prev.get("time_of_day") or "") != (cur.get("time_of_day") or ""):
        return None
    if any(w in ((cur.get("transitions") or {}).get("in") or "") for w in _SOFT_CUT):
        return None
    # Different people, different picture. C01S02 is a close-up on 龚光杰's
    # hand and face, C01S03 the same size and angle on 褚姓少年 - so C01S03
    # inherited 龚光杰 and h3 kept his face for the whole shot: a sixty-year-old
    # in blue where a pale twenty-year-old in green should be (2026-09-30).
    # h3 pins frame 0; whoever is in it is who the shot is about.
    # So the new shot's cast must be people the old frame already HAS - a
    # subset, not the same list: C01S01 (the hall, eight ids) -> C01S02 (the
    # duel and four of the watchers) -> C01S03 (the two blades) is one
    # continuous fight, and requiring equal lists started both cold, where
    # Flux redrew the fighters from text - two men became four, the blue
    # tunic became brown (2026-10-01).
    if not set(cur.get("characters") or []) <= set(prev.get("characters") or []):
        return None
    pc, cc = prev.get("camera") or {}, cur.get("camera") or {}
    if (pc.get("angle") or "") != (cc.get("angle") or ""):
        return None
    a = _SHOT_SCALE.get(pc.get("shot_size") or "")
    b = _SHOT_SCALE.get(cc.get("shot_size") or "")
    if a is None or b is None:
        return None
    if b > a + 1e-9:
        return None
    # A CENTRE crop cannot find a subject. Pushing in from a hall of eight to
    # a close-up of one of them crops whoever stands in the middle - C01S11
    # (褚 alone) inherited C01S10's centre and opened on 12 people and a
    # second young man in green; C01S06 would have opened on the duel instead
    # of 段誉 (2026-10-04). When the cast shrinks, only a mild push keeps the
    # picture; a real punch-in onto fewer people starts from its own first
    # frame, built on those people's sheets.
    sub = set(cur.get("characters") or [])
    if len(sub) < len(set(prev.get("characters") or [])) and b / a < 0.6:
        return None
    return 1.0 if abs(b - a) < 1e-9 else max(0.25, b / a)


def _crop_frame(src, dst, ratio):
    """Centre-crop a tail frame to the next shot's framing, biased upward.

    Faces sit above centre, so a straight centre crop of a person tends to cut
    the head off; 0.4 keeps the crop a little high.
    """
    _ff(["-i", src, "-vf",
         "crop=iw*%.4f:ih*%.4f:(iw-iw*%.4f)/2:(ih-ih*%.4f)*0.4"
         % (ratio, ratio, ratio, ratio),
         "-frames:v", "1", "-q:v", "2", dst], timeout=180)


# How long one h3 segment may take before the batch calls it wedged, and how
# many times a shot is re-rendered before it is given up on. h3_server has its
# own ceiling; this one sits just above it so its clearer message wins the
# race. 2026-09-10: a GPU wedge inside the text encoder held one shot for four
# hours and the other 113 never started.
#
# These are calibrated to how long a HEALTHY segment takes, so they move when
# the canvas does. 90 frames at 1312x736 ran ~27 min and 90/100 min gave that
# 3.3x headroom; 1920x1088 is 2.02x the pixels and cost is frames x pixels, so
# a healthy segment is now ~55 min and the old ceilings would have killed
# nearly every one of them as wedged. 150/165 keeps the same headroom.
VIDEO_STALL = int(os.environ.get("BOOKREEL_VIDEO_STALL", str(100 * 60)))
VIDEO_RETRIES = 2
# The voice is not a side pass. tts_server carries oom_score_adj=1000 ON
# PURPOSE - it is what the kernel takes instead of the render - so some
# mornings it is simply dead: 2026-10-10 06:10 it was (OOM, pid 104859), and
# from then on every line of every shot came back "Connection refused",
# _voice_track answered (None, 0.0, []) as if the shot had nothing to say,
# and all twelve dialog shots of ch01 were rendered TWICE with a talking mouth
# and no sound, logged "无台词". So when :9100 does not answer the render now
# restarts it and waits this long for /health; a shot with lines is never
# rendered mute - if the service stays down the QUEUE stops with the shot
# back at its head (VoiceUnavailable), and nothing downstream inherits.
TTS_START_WAIT = int(os.environ.get("BOOKREEL_TTS_START_WAIT", "300"))


class VoiceUnavailable(RuntimeError):
    """The tts service could not be brought back: stop the queue, keep the shot."""
# The clip checks (head count after segment 1 and on the finished clip)
# ADVISE by default, like the image checker: the clip is published with its
# problems on the take (check.problems) and the person decides - 认可, or a
# 重出意见 and 重出. =1 restores the old behaviour: a failed count throws the
# clip away and the retry path re-renders it (~2 GPU-hours a go).
# ON since 2026-09-30 ("make sure all the clips follow the number of actors"):
# C01S02, a two-man duel, came back with four people fighting and was
# published with a note. A wrong head count is now re-rendered on a new seed.
# OFF again since 2026-10-01 ("是否准确应该由我决定，你如果认为有错误，就把错误
# 版本保存，不对的话，我来删除"): the checker threw away three correct C01S01
# takes for a question it asked wrong. A flagged clip is published as a take
# with its problems on it; the person deletes or 重出s it.
CLIP_AUTO_FIX = os.environ.get("BOOKREEL_CLIP_AUTO_FIX", "0") not in ("0", "", "false")


def _clip_notes_path(slug):
    return os.path.join(PROJECTS, slug, "assets", "clip_notes.json")


def clip_note(slug, key):
    return str((_read_json(_clip_notes_path(slug), {}) or {}).get(key) or "").strip()


class VideoBatch:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "video_batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.state = _read_json(self.path, None) or {"status": "idle", "queue": [], "done": [], "failed": []}
        # Last rendered shot: {"key", "shot", "png"}. The next shot inherits
        # that png as its first frame when the script says they run together.
        self._prev = None

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
        st["running"] = bool(self.thread and self.thread.is_alive())
        st["stop_requested"] = self.stop_flag
        return st

    def _log(self, msg):
        with self.lock:
            self.state.setdefault("log", []).append({"t": int(time.time()), "msg": msg})
            self.state["log"] = self.state["log"][-80:]
            self._save()

    # -- progress ------------------------------------------------------
    # A SEGMENT is the unit of work here, not a shot: every one of them is 90
    # frames on one canvas at 4 steps and costs the same ~27 GPU-minutes,
    # while a shot is anywhere from one segment to five. The page used to
    # measure the current shot by the sampler's STEP count, which restarts at
    # every segment - so a four-segment shot read 95% done when it was 25%
    # done, four times over, and the bar sat near 100% with an hour and a half
    # still to run. Publish where we really are: segments finished of this
    # shot, and how long a segment has actually been taking.
    def _mark(self, seg=None, segs=None, minutes=None):
        with self.lock:
            if segs is not None:
                self.state["segs"] = int(segs)
            if seg is not None:
                self.state["seg"] = int(seg)
            if minutes is not None:
                # A mean of the last few, not of all time: the pace moves with
                # the canvas and with whatever else is on the GPU.
                hist = (self.state.get("seg_history") or [])[-7:] + [round(float(minutes), 2)]
                self.state["seg_history"] = hist
                self.state["seg_minutes"] = round(sum(hist) / len(hist), 2)
            self._save()

    def _with_next(self, keys):
        """The keys plus, for each, the shot right after it in the script when
        that shot already has a clip - it was cut against the version being
        replaced (2026-10-01: 按镜号插队，并自动重出后一镜). One step only: the
        shot added here does not pull in ITS successor."""
        out = list(keys)
        for k in keys:
            try:
                n, sid = k.split(":", 1)
                sc = _read_json(os.path.join(PROJECTS, self.slug, "script", "%02d.json" % int(n)), {}) or {}
            except (ValueError, TypeError):
                continue
            ids = [x.get("id") for x in sc.get("shots") or []]
            if sid in ids[:-1]:
                nxt = ids[ids.index(sid) + 1]
                nk = "%s:%s" % (n, nxt)
                if nk not in out and os.path.isfile(os.path.join(
                        PROJECTS, self.slug, "clips", "%02d" % int(n), "%s.mp4" % nxt)):
                    out.append(nk)
        return out

    def start(self, keys, front=False, fmt=None):
        if not front:
            keys = self._with_next(keys)
        with self.lock:
            if self.thread and self.thread.is_alive():
                if front:
                    # a click from the page: waiting keys move up, the shot in
                    # flight is rendered again after it (its note may be new).
                    # Only for ONE key: a bulk "fights first" reorder that
                    # happened to name the shot in flight rendered it twice
                    # (C01S01, 2026-09-29 and -30).
                    if len(keys) > 1:
                        keys = [k for k in keys if k != self.state.get("current")]
                    self.state["queue"] = [k for k in self.state["queue"] if k not in set(keys)]
                    self.state["queue"][0:0] = list(keys)
                else:
                    # In SCRIPT order among the waiting shots (2026-10-01; this
                    # replaced "to the BACK" of 09-30): a shot starts from the
                    # published end of the one before it, so a 重出 of S03 must
                    # come before the S04 it now feeds. The shot in flight is
                    # queued again (a 重出 of it).
                    have = set(self.state["queue"])
                    self.state["queue"].extend(k for k in keys if k not in have)
                    self.state["queue"].sort(key=_shot_order)
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"]),
                        "format": self.state.get("format")}
            # A new run goes in SCRIPT order: each shot starts from the
            # published end of the one before it, so that one has to exist
            # first (2026-10-01).
            keys = sorted(keys, key=_shot_order)
            self.state = {"status": "running", "queue": list(keys), "done": [], "failed": [],
                          "format": fmt if fmt in VIDEO_FORMATS else "16:9",
                          "started": int(time.time()), "current": None, "current_started": None,
                          "last_error": "", "log": []}
            self._save()
            self.stop_flag = False
            self._prev = None
            self.thread = threading.Thread(target=self._run, name="video-" + self.slug, daemon=True)
            self.thread.start()
            return {"ok": True, "queued": len(keys)}

    def resume(self):
        st = self.state
        if st.get("status") == "running" and (st.get("queue") or st.get("current")):
            if st.get("current") and st["current"] not in st["queue"]:
                st["queue"].insert(0, st["current"])
            st["current"] = None
            self._log("服务重启，续跑：剩余 %d 镜" % len(st["queue"]))
            # The old process's h3 task is still sampling for nobody, ahead of
            # this process's resubmission of the same segment - the orphan the
            # image queue already learned to kill (Batch.resume). Drop it.
            if st.get("task_id"):
                self._cancel(st["task_id"])
                st["task_id"] = None
            self.stop_flag = False
            # The tail frame lives in this process, so a restart breaks the
            # chain: the first shot after a resume falls back to its plate
            # rather than inheriting a frame nobody kept.
            self._prev = None
            self.thread = threading.Thread(target=self._run, name="video-" + self.slug, daemon=True)
            self.thread.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        self._log("收到停止：当前这镜出完就停")

    def _script_prev(self, key):
        """(key, shot) of the shot right BEFORE this one in the script, or
        (None, None). Script order, not shot numbers: tianlongbabu has no
        C01S20, so S19 -> S21 is one cut and `_adjacent` called it a gap."""
        try:
            n, sid = key.split(":", 1)
            sc = _read_json(os.path.join(PROJECTS, self.slug, "script", "%02d.json" % int(n)), {}) or {}
        except (ValueError, TypeError):
            return None, None
        shots = sc.get("shots") or []
        ids = [x.get("id") for x in shots]
        if sid not in ids or ids.index(sid) == 0:
            return None, None
        p = shots[ids.index(sid) - 1]
        return "%s:%s" % (n, p.get("id")), p

    def _published_prev(self, key, work):
        """The previous shot as it is PUBLISHED: its live clip's last frame.

        Clips play in script order and the end of one may be the start of the
        next (2026-10-01: "因为clip是有前后关系的…前一片的末尾可能是下一CLIP的
        开头"). The in-memory tail this replaces broke the chain three ways: it
        was empty after every web_server restart and after a 重出 of one shot
        alone, so those shots opened cold; and it was the RAW last segment's
        frame, while a fight clip is graded - undercranked, its frozen tail
        cut - so the next shot started from a moment the film never shows."""
        pk, pshot = self._script_prev(key)
        if not pk:
            return None
        n = int(key.split(":", 1)[0])
        clip = os.path.join(PROJECTS, self.slug, "clips", "%02d" % n, "%s.mp4" % pshot.get("id"))
        if not os.path.isfile(clip):
            return None
        png = os.path.join(work, "%s-end.png" % pshot.get("id"))
        try:
            if not os.path.isfile(png) or os.path.getmtime(png) < os.path.getmtime(clip):
                _last_frame(clip, png)
        except Exception as exc:                            # noqa: BLE001
            self._log("！%s 取不到上一镜 %s 的末帧：%s" % (key, pk, str(exc)[:90]))
            return None
        take = {}
        try:
            td = os.path.join(PROJECTS, self.slug, "clips", "%02d" % n, "_takes", pshot.get("id"))
            act = (_read_json(os.path.join(td, "_active.json"), {}) or {}).get("take")
            take = _read_json(os.path.join(td, "%s.json" % act), {}) or {} if act else {}
        except Exception:                                   # noqa: BLE001
            take = {}
        return {"key": pk, "shot": pshot, "png": png, "take": take}

    def _shot(self, key):
        n, sid = key.split(":", 1)
        sc = _read_json(os.path.join(PROJECTS, self.slug, "script", "%02d.json" % int(n)), {}) or {}
        return int(n), next((s for s in sc.get("shots", []) if s.get("id") == sid), None)

    def _run(self):
        while True:
            with self.lock:
                if self.stop_flag or not self.state["queue"]:
                    self.state["status"] = "stopped" if self.stop_flag else (
                        "done" if not self.state["failed"] else "done_with_failures")
                    self.state["current"] = None
                    self.state["finished"] = int(time.time())
                    self._save()
                    return
                key = self.state["queue"].pop(0)
                # one render per key: drop any other copy still waiting
                self.state["queue"] = [k for k in self.state["queue"] if k != key]
                self.state["current"] = key
                self.state["current_started"] = int(time.time())
                self.state["seg"], self.state["segs"] = 0, 0
                self.state["attempt"], self.state["attempts"] = 1, VIDEO_RETRIES + 1
                self.state["retry_reason"], self.state["retry_lost_minutes"] = "", 0
                self.state["retry_seg"], self.state["retry_segs"] = 0, 0
                self._save()
            try:
                # A shot used to get exactly one attempt, so one wedged render
                # left a hole in the finished video. Retry it - with a fresh
                # seed, since the default 0 would reproduce the same wedge -
                # and only then give up. A stop is never retried.
                for attempt in range(VIDEO_RETRIES + 1):
                    t_try = time.time()
                    with self.lock:
                        self.state["attempt"] = attempt + 1
                        self.state["attempts"] = VIDEO_RETRIES + 1
                        self._save()
                    try:
                        self._render(key, seed=None if attempt == 0 else random.randrange(1, 2**31))
                        with self.lock:
                            self.state["done"].append(key)
                        self._film(key)
                        break
                    except VoiceUnavailable:
                        raise
                    except Exception as exc:                # noqa: BLE001
                        if self.stop_flag or attempt == VIDEO_RETRIES:
                            raise
                        # A retry is not a footnote: it throws away the
                        # segments already sampled - up to an hour of GPU -
                        # and the bar goes backwards while the clock goes
                        # forwards. Say how far it got, what stopped it and
                        # what that cost, in the progress state and not only
                        # in the log, so the answer to "why has this shot
                        # been running for two hours" is on screen.
                        with self.lock:
                            lost = (time.time() - t_try) / 60.0
                            self.state["retry_reason"] = str(exc)[:200]
                            self.state["retry_seg"] = int(self.state.get("seg") or 0)
                            self.state["retry_segs"] = int(self.state.get("segs") or 0)
                            self.state["retry_lost_minutes"] = round(lost, 1)
                            self.state["retry_at"] = int(time.time())
                            self.state["retries_total"] = int(self.state.get("retries_total") or 0) + 1
                            self._save()
                        self._log("↻ %s 第 %d/%d 次重出（换种子）：跑到第 %d/%d 段断了，"
                                  "丢了 %.0f 分钟 · %s"
                                  % (key, attempt + 2, VIDEO_RETRIES + 1,
                                     int(self.state.get("retry_seg") or 0),
                                     int(self.state.get("retry_segs") or 0),
                                     lost, str(exc)[:120]))
            except VoiceUnavailable as exc:
                # Not a failed shot - a dead service. The shot goes back to
                # the head of the queue and the queue stops, so nothing after
                # it is rendered mute either; the person restarts tts (or the
                # render does, next start) and presses 开始 again.
                with self.lock:
                    if key not in self.state["queue"]:
                        self.state["queue"].insert(0, key)
                    self.state["last_error"] = str(exc)[:300]
                    self.stop_flag = True
                self._log("× %s：%s" % (key, str(exc)[:200]))
                self._prev = None
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["failed"].append({"key": key, "error": str(exc)[:300], "t": int(time.time())})
                    self.state["last_error"] = str(exc)[:300]
                self._log("× %s：%s" % (key, str(exc)[:160]))
                # Nothing downstream may inherit across a hole: the next shot
                # would otherwise open on the tail of the shot before THIS
                # one, a different moment - and when this shot failed its
                # head count, that tail is the frame we refused to publish.
                self._prev = None
            finally:
                with self.lock:
                    self.state["current"] = None
                    self.state["seg"], self.state["segs"] = 0, 0
                    self._save()

    def _film(self, key):
        """Join the chapter as soon as its last clip lands.

        Only when the chapter is COMPLETE: a film of half a chapter would be
        rebuilt on every shot, and each rebuild rewrites the whole file. It
        must never take the queue down with it either - a chapter that will not
        join is a thing to read in the log, not a reason to stop rendering the
        next 140 shots.
        """
        try:
            n = int(str(key).split(":", 1)[0])
        except ValueError:
            return
        try:
            parts, total = chapter_clips(self.slug, n)
            if not total or len(parts) != total:
                return
            # …and only once nothing of this chapter is still queued: a
            # re-take of eight shots used to rebuild (and now archive) the
            # film eight times over.
            with self.lock:
                left = [k for k in (self.state.get("queue") or []) if str(k).split(":", 1)[0] == str(n)]
            if left:
                return
            info = build_chapter_film(self.slug, n)
            if info.get("built"):
                self._log("🎬 第 %d 章成片：%d 段，%.1f 秒，%.0f MB"
                          % (n, info["clips"], info["seconds"],
                             os.path.getsize(info["path"]) / 1e6))
            # An imported novel: once every part of one ORIGINAL chapter has
            # its film, the book's chapter is joined too.
            for b in build_book_films(self.slug, only_part=n):
                self._log("🎬 原书第 %d 章《%s》：%d 段，%.0f 秒"
                          % (b["book"], b["title"], b["parts"], b["seconds"]))
        except Exception as exc:                            # noqa: BLE001
            self._log("× 第 %d 章合成失败：%s" % (n, str(exc)[:160]))

    def _grid_seconds(self, secs, fps=24.0):
        """Round up to h3's 5+17n frame grid, the way h3 itself does."""
        want = float(secs) * fps
        k = 0
        while 5 + 17 * k < want:
            k += 1
        return (5 + 17 * k) / fps

    def _segment(self, key, body, ref, label):
        """One h3 call: submit, wait, return (bytes, query)."""
        b = dict(body)
        if ref:
            b["first_frame_image"] = "http://127.0.0.1:%d%s" % (WEB_PORT[0], ref)
        else:
            b.pop("first_frame_image", None)
        # Same hop for the control video: h3 fetches it back off this server,
        # and _media already serves .mp4.
        if b.get("control_video", "").startswith("/"):
            b["control_video"] = "http://127.0.0.1:%d%s" % (WEB_PORT[0], b["control_video"])
        tid = None
        while tid is None:
            if self.stop_flag:
                raise RuntimeError("停止")
            try:
                req = urllib.request.Request(
                    H3 + "/v1/video/generations", data=json.dumps(b).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    tid = json.load(r)["task_id"]
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["last_error"] = "视频服务不可达，30 秒后重试：%s" % str(exc)[:120]
                    self._save()
                time.sleep(30)
        with self.lock:
            self.state["task_id"] = tid
            self.state["task_key"] = key
            self.state["last_error"] = ""
            self._save()
        t0 = time.time()
        while True:
            time.sleep(10)
            if self.stop_flag:
                raise RuntimeError("停止")
            # The deadline is checked here, before the poll, and not inside the
            # "not finished yet" branch: when the video service is unreachable
            # the poll below raises and its `continue` used to skip the check
            # altogether, so an h3 that never came back held the queue open for
            # good. Cancel the task too, or it keeps the GPU while we retry.
            if time.time() - t0 > VIDEO_STALL:
                self._cancel(tid)
                raise RuntimeError("%s 等了 %d 分钟没有结果"
                                   % (label, VIDEO_STALL // 60))
            try:
                with urllib.request.urlopen(
                        H3 + "/v1/query/video_generation?task_id=" + tid, timeout=60) as r:
                    q = json.load(r)
            except urllib.error.HTTPError as exc:
                # h3 keeps its tasks in memory, so a 404 means it was restarted
                # and this render is gone. Waiting out the deadline for that is
                # an hour and a half of nothing; re-render it now instead.
                if exc.code == 404:
                    raise RuntimeError("%s：视频服务重启过，任务已丢失" % label)
                continue
            except Exception:                               # noqa: BLE001
                continue
            st = q.get("status")
            if st == "Fail":
                raise RuntimeError(str(q.get("error"))[:200])
            if st != "Success":
                continue
            fid = q.get("file_id")
            if not fid:
                raise RuntimeError("%s 完成但没有文件" % label)
            with urllib.request.urlopen(H3 + "/v1/files/" + fid, timeout=600) as r:
                return r.read(), q

    def _cancel(self, tid):
        """Tell h3 to drop a task we have stopped waiting for."""
        try:
            req = urllib.request.Request(
                H3 + "/v1/video/cancel", data=json.dumps({"task_id": tid}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).close()
        except Exception:                                   # noqa: BLE001
            pass

    def _voice_track(self, work, key, shot):
        """Synthesize everything heard in this shot into one wav.

        Returns (path, seconds, cues); (None, 0.0, []) ONLY when there is
        nothing to say. A silent shot is a normal outcome - plenty of shots are
        pure action - but a shot WITH lines and no voice is not: it used to be
        (":9100 down" was logged and the shot went on without its sound, and
        the picture was then a talking mouth over nothing, 2026-10-10..15).
        Nothing is lost by refusing here - the voice is step 1, before any GPU
        is spent - so a dead service is restarted and waited for, and if it
        stays dead the shot raises VoiceUnavailable and the queue stops.
        """
        lines = _speech_lines(shot, _casting(self.slug))
        if not lines:
            return None, 0.0, []
        parts, cues, t = [], [], 0.0
        for i, (voice, text, instruct) in enumerate(lines):
            wav = os.path.join(work, "%s-voice-%d.wav" % (shot["id"], i))
            data = self._synth_or_stop(key, i, text, voice, instruct)
            with open(wav, "wb") as f:
                f.write(data)
            # The shot review, voice half: does the line SAY what the script
            # says? A garbled synthesis is cheap to redo and two GPU-hours of
            # picture under it are not.
            for again in range(2):
                ok_l, heard, sc = shotqa.line_ok(wav, text, _asr_text)
                if ok_l:
                    break
                self._log("   %s 第 %d 句听着不对（%.0f%%：%s），重念"
                          % (key, i + 1, (sc or 0) * 100, heard[:24]))
                try:
                    data = _synth(text, voice, instruct)
                    with open(wav, "wb") as f:
                        f.write(data)
                except Exception:                           # noqa: BLE001
                    break
            parts.append(wav)
            # Cues are measured off the wav that actually goes into the clip.
            # The page used to synthesize its own copy of the narration and
            # align that instead, so the subtitles described a different take
            # of the same line - close enough to look right and wrong enough
            # to drift, and dialog got no subtitle at all.
            #
            # Spread the text across the SPEECH, not across the file: the line
            # is laid end to end with its neighbours, so the clock still runs
            # on the full duration, but the words inside it start where the
            # first sound is. Asking the aligner instead was measured and is
            # worse - fa-zh put the last sentence of C01S01 at 7.53-9.29 s
            # against a real 9.02-10.68 s (2026-09-13), and it can fail in the
            # middle of a ninety-minute render, which this cannot.
            d = _duration(wav)
            # A wav with no length is a synthesis that did not happen - the
            # service answered 200 with nothing, or the write was cut short.
            # Left alone it contributes no audio and a fistful of cues all
            # timed 0.00-0.00, which is a subtitle for a line nobody says.
            # Treat it the same as a refused synthesis.
            if d <= 0.0:
                raise RuntimeError("%s 第 %d 句合成为空（%s）：%s —— 不出哑巴镜头"
                                   % (key, i + 1, voice, text[:40]))
            lead, tail, gaps = _speech_map(wav)
            cues.extend(_split_cue(text, round(t + lead, 3), round(t + tail, 3),
                                   [(t + a, t + z) for a, z in gaps]))
            t += d
        out = os.path.join(work, "%s-voice.wav" % shot["id"])
        if len(parts) == 1:
            os.replace(parts[0], out)
        else:
            _concat_stream(parts, out)
            for q in parts:
                try:
                    os.unlink(q)
                except OSError:
                    pass
        return out, _duration(out), cues

    def _synth_or_stop(self, key, i, text, voice, instruct):
        """One line's wav, or an exception - never silence for a line.

        A service that does not answer at all (refused, timed out) is brought
        back with `launch.sh restart-one tts` and waited for; if it will not
        come back the queue stops (VoiceUnavailable). A service that is up but
        refuses THIS line (HTTP 4xx/5xx: CosyVoice's own assertion, a bad
        voice pair) is asked once more and then fails this SHOT only, so the
        queue goes on and the person sees which line it was.
        """
        try:
            return _synth(text, voice, instruct)
        except urllib.error.HTTPError as exc:
            self._log("！%s 第 %d 句语音服务拒绝（%s）：HTTP %s，再试一次"
                      % (key, i + 1, voice, exc.code))
            try:
                return _synth(text, voice, instruct)
            except Exception as exc2:                       # noqa: BLE001
                raise RuntimeError("%s 第 %d 句出不了声（%s）：%s —— 不出哑巴镜头"
                                   % (key, i + 1, voice, str(exc2)[:90]))
        except Exception as exc:                            # noqa: BLE001
            self._log("！%s 第 %d 句连不上语音服务（%s）：%s" % (key, i + 1, voice, str(exc)[:90]))
        if not self._tts_recover(key):
            raise VoiceUnavailable("语音服务 :9100 起不来，%s 的台词出不了声；队列已停，这一镜留在队首"
                                   % key)
        try:
            return _synth(text, voice, instruct)
        except Exception as exc:                            # noqa: BLE001
            raise VoiceUnavailable("语音服务重启后仍答不出 %s 第 %d 句：%s；队列已停，这一镜留在队首"
                                   % (key, i + 1, str(exc)[:90]))

    def _tts_recover(self, key):
        """Bring :9100 back and wait (bounded) for its /health. True when up."""
        if _tts_up():
            return True
        self._log("   %s 语音服务没起来，./launch.sh restart-one tts，最多等 %d 秒"
                  % (key, TTS_START_WAIT))
        try:
            subprocess.run([os.path.join(ROOT, "launch.sh"), "restart-one", "tts"],
                           capture_output=True, timeout=180)
        except Exception as exc:                            # noqa: BLE001
            self._log("！%s restart-one tts 没跑成：%s" % (key, str(exc)[:90]))
        t0 = time.time()
        while time.time() - t0 < TTS_START_WAIT:
            if self.stop_flag:
                return False
            if _tts_up():
                self._log("   %s 语音服务回来了（%.0f 秒）" % (key, time.time() - t0))
                return True
            time.sleep(5)
        return False

    def _drop_first_frame(self, shot, cold):
        """A cold shot's own first frame goes with a failed review: the retry
        must redraw it on a new seed, or it pins the same wrong people."""
        if not cold:
            return
        try:
            f = first_frame_path(self.slug, shot)
            if os.path.isfile(f):
                _keep_rejected(f, shot, 0, "片子核对没过，首帧随之重画", "", None, [])
            if os.path.isfile(f + ".json"):
                os.unlink(f + ".json")
            self._log("   %s 首帧存档到 _rejected，重试时重画" % shot.get("id"))
        except Exception:                                   # noqa: BLE001
            pass

    def _render(self, key, seed=None):
        _hot_reload(self._log)
        n, shot = self._shot(key)
        if not shot:
            raise RuntimeError("script 里没有 %s" % key)
        if not (shot.get("video_prompt") or shot.get("video_prompt_raw") or shot.get("slug_line")):
            raise RuntimeError("这一镜没有 video_prompt")
        # Built here, not read off the script: the script on disk stays what the
        # model wrote, and a re-render picks up any change to the prompt rules
        # without regenerating chapters. _film_prompt is what keeps the
        # label block out of the picture.
        fmt = self.state.get("format") or "16:9"
        bp = _read_json(os.path.join(PROJECTS, self.slug, "blueprint.json"), {}) or {}
        # One costume per person: "月白或浅青色" let every shot pick its own.
        bp = filmwords.one_costume_bp(bp)
        # What the shot's SCENE holds (a crowd off screen is still in the room)
        _tag_scene(shot, (_read_json(os.path.join(PROJECTS, self.slug, "script", "%02d.json" % n),
                                     {}) or {}).get("shots"), bp)
        prompt = _film_prompt(shot, bp, fmt)
        if not prompt:
            raise RuntimeError("这一镜没有 video_prompt")
        # The person's 重出意见 for this shot. Into the Flux first frame as
        # 【必须】 (Flux does not paint captions) and into every segment as plain
        # prose - brackets and labels are what h3 paints (CLAUDE.md).
        user_note = clip_note(self.slug, key)
        note_prose = filmwords.dedigit(_denames(re.sub(r"[【】\[\]:：]", "", user_note), bp)) if user_note else ""
        problems, remarks = [], []
        lib = _read_json(os.path.join(PROJECTS, self.slug, "assets", "prompts.json"), {}) or {}
        base = os.path.join(PROJECTS, self.slug)
        work = os.path.join(base, "clips", "%02d" % n, "_frames")
        os.makedirs(work, exist_ok=True)
        t0 = time.time()

        # 1. What has to be heard decides how long the picture must be. The
        #    script asks for 10 s a shot and the narration for these runs 10-13
        #    s, so a 4 s clip was never going to carry its own line.
        voice, voice_secs, cues = self._voice_track(work, key, shot)
        try:
            _cast = _casting(self.slug)
            _have = set(_voice_pairs())
            for r_ in shotqa.check_voices(ROOT, shot, bp, _cast,
                                          lambda who: _voice_for(who, _cast, _have)):
                remarks.append(r_)
                self._log("⚠ %s 声音：%s" % (key, r_))
        except Exception as exc:                            # noqa: BLE001
            self._log("！%s 声音核对没做成：%s" % (key, str(exc)[:100]))
        # Cold or inherited is decided HERE, before the length: a cold start
        # with dialog delays the sound (COLD_LEAD_SECONDS), and the picture has
        # to be long enough for the delayed sound.
        prev = self._published_prev(key, work)
        ratio = _inherit_ratio(prev["shot"], shot) if prev else None
        if (voice and ratio is None and spoken(shot) and cues
                and COLD_LEAD_SECONDS > 0 and float(cues[0]["start"]) < COLD_LEAD_SECONDS):
            lead = COLD_LEAD_SECONDS - float(cues[0]["start"])
            try:
                led = os.path.join(work, "%s-voice-lead.wav" % shot["id"])
                _ff(["-i", voice, "-af", "adelay=%d:all=1" % int(round(lead * 1000)), led],
                    timeout=120)
                voice, voice_secs = led, voice_secs + lead
                cues = [dict(c, start=round(c["start"] + lead, 3), end=round(c["end"] + lead, 3))
                        for c in cues]
                self._log("   %s 冷起步有台词：声音后延 %.2f 秒，等人进画" % (key, lead))
            except Exception as exc:                        # noqa: BLE001
                self._log("！%s 声音后延没做成（照常）：%s" % (key, str(exc)[:90]))
        seg = self._grid_seconds(SEG_SECONDS)
        # Length comes from what the shot carries - never a flat floor, and
        # never shorter than the line: the mux is -shortest, so cutting the
        # picture here is what cut the narration mid-word.
        if voice_secs > 0:
            # …and never shorter than its action: one 着 is 0.5 s of sound and
            # C01S07 has three beats of sword play to show around it.
            target = max(VIDEO_MIN_SECONDS,
                         min(VIDEO_VOICE_MAX_SECONDS,
                             voice_secs + VIDEO_HANDLE_SECONDS),
                         stage.beats_seconds(shot, seg))
        else:
            wide = str((shot.get("camera") or {}).get("shot_size") or "").upper() in ("ELS", "LS")
            target = max(VIDEO_MIN_SECONDS,
                         VIDEO_SILENT_WIDE_SECONDS if wide else VIDEO_SILENT_SECONDS,
                         stage.beats_seconds(shot, seg))
        nseg = 1
        while nseg * seg < target - 1e-6:
            nseg += 1

        # 2. Where the first segment starts. Inheriting the previous shot's
        #    last frame is what stops 108 clips from each opening on the same
        #    location plate, which is what made them look isolated.
        # How many people this shot may contain: said in the prompt
        # (_people_clause), drawn into the generated first frame, and checked
        # in the picture. Defined here because everything below reads it.
        want_heads = cast_count(shot, bp)
        if want_heads < CROWD and _crowd_bg_clause(shot, bp):
            # The hall behind this close-up is asked for on purpose
            # (`_crowd_bg_clause`), so a ceiling of one is a check against
            # the prompt's own words: C01S06 was rendered seven times over
            # "数到 30 个人，应为 1" (2026-10-14). Only the fighter count and
            # the review still apply.
            want_heads = CROWD
            self._log("   %s 身后要有满厅的人，不按人数上限核对" % key)
        ref, note = None, ""

        def _cold_frame():
            """The plate path, as a function.

            It used to be only the `else` of the inheritance test, which left
            a failed inherited frame with nowhere to go - see the fall-back
            below.
            """
            r, nt, pl = _pick_first_frame(self.slug, shot, lib)
            # A cold shot builds its own first frame from that plate's view,
            # with the subject in it and the head count verified, so h3 is
            # never the party deciding how many people the scene has.
            if FIRSTFRAME and pl and want_heads >= 1:
                try:
                    cont = prev["png"] if (prev and _continuity_ref(prev["shot"], shot,
                                                                   prev.get("take"))) else ""
                    if cont:
                        self._log("   %s 首帧以 %s 末帧为参考（人物与服装接续）" % (key, prev["key"]))
                    ffp = ensure_first_frame(self.slug, shot, bp, pl, want_heads,
                                             stop=lambda: self.stop_flag, log=self._log,
                                             note=user_note, cont=cont)
                except Exception as exc:                    # noqa: BLE001
                    ffp, _ = "", self._log("！%s 首帧没生成（改用场景图）：%s" % (key, str(exc)[:110]))
                if ffp:
                    rel2 = os.path.relpath(ffp, base).replace(os.sep, "/")
                    r = "/media/%s/%s" % (self.slug, urllib.parse.quote(rel2))
                    nt = "本镜首帧（据 %s 生成）" % pl.get("key")
            return r, nt, pl

        def _frame_file(r):
            fp = os.path.join(base, urllib.parse.unquote(
                r.split("/media/%s/" % self.slug, 1)[-1])) if (r and "/media/" in r) else ""
            return fp if (fp and os.path.isfile(fp)) else ""

        if ratio is not None:
            src = prev["png"]
            if ratio < 1.0:
                src = os.path.join(work, "%s-inherit.png" % shot["id"])
                _crop_frame(prev["png"], src, ratio)
            rel = os.path.relpath(src, base).replace(os.sep, "/")
            ref = "/media/%s/%s" % (self.slug, urllib.parse.quote(rel))
            note = "接 %s 末帧%s" % (prev["key"],
                                     "" if ratio >= 1.0 else "（推近 %.0f%%）" % (ratio * 100))
        else:
            ref, note, plate = _cold_frame()
        # COLD means the pinned first frame is a location plate - an image
        # Flux drew from a text description, whose camera nobody knows. The
        # previz camera is `solve_camera`'s, from the shot's own size, angle
        # and lens, and the two have nothing to do with each other: measured
        # on C01S02 (2026-09-20) the plate was a wide empty courtyard and the
        # control was one seated body filling 40% of frame height, and h3
        # resolved the contradiction by pushing in AND painting the subject
        # twice, side by side, both writing. That is the same rule the
        # inherited path already obeys through `crop=` - a control video that
        # disagrees with the first frame is worse than no control video - and
        # here it cannot be made to agree by cropping, because there is no
        # ratio, only a different camera. So a cold shot renders the way it
        # did before previz existed, until its first frame is generated AT the
        # previz camera.
        cold = ratio is None

        # 2a. Count the people in the FIRST FRAME, before a single GPU-minute
        #     is spent. h3 pins that frame: anybody already standing in it is
        #     in the finished clip whatever the prompt says, and no re-roll of
        #     the seed can remove them. C01S01 burned 3 attempts x 134 min
        #     failing its head count on a plate that has a man in the alley -
        #     a question worth 3 seconds answered after 6.6 GPU-hours. A shot
        #     that fails here is a picture to fix, not a shot to re-render, so
        #     the error names the file.
        if CLIP_CHECK and ref and want_heads >= 0:
            fpath = _frame_file(ref)
            if fpath:
                ok_ff, n_ff = image_headcount(fpath, want_heads)
                # An INHERITED frame that fails the count is not a picture to
                # fix: it is the previous shot's last frame, and it is correct
                # for THAT shot. This one simply cannot open on it - C01S29
                # follows C01S28 in the same courtyard at the same hour, so it
                # inherited a frame with a man in it while its own cast is
                # empty. Raising there threw the whole shot away and left a
                # hole in the chapter (C01S17 on 2026-09-21, C01S29 on 09-22),
                # when the cold path standing right beside it renders fine -
                # and is what this shot would have taken had it been queued on
                # its own. So fall through to the plate, drop the control
                # video with it (`cold`), and only fail if the frame this shot
                # draws for ITSELF is also wrong, which is the case the error
                # was written for.
                if ok_ff is False and ratio is not None:
                    self._log("   %s 接来的首帧里有 %d 个人，这一镜只该有 %d 个："
                              "改成冷开场，自己出首帧" % (key, n_ff, want_heads))
                    ratio, cold = None, True
                    ref, note, plate = _cold_frame()
                    fpath = _frame_file(ref)
                    ok_ff, n_ff = image_headcount(fpath, want_heads) if fpath else (None, None)
                # The checker advises (2026-10-01): ensure_first_frame already
                # kept every flagged try and USED the last one, so raising here
                # threw the shot away anyway - C01S03 failed outright and the
                # chain jumped to C01S04. Record it on the take and render.
                if ok_ff is False and not CLIP_AUTO_FIX:
                    problems.append("首帧数到 %d 个人，应为 %d 个（%s）" % (
                        n_ff, want_heads, os.path.relpath(fpath, base)))
                    self._log("⚠ %s 首帧有 %d 个人，应为 %d 个：照出，由你决定"
                              % (key, n_ff, want_heads))
                elif ok_ff is False:
                    raise RuntimeError(
                        "首帧里就有 %d 个人，这一镜只该有 %d 个：%s —— 换种子没用，"
                        "要重出这张图" % (n_ff, want_heads,
                                          os.path.relpath(fpath, base)))
                if ok_ff is True and n_ff is not None:
                    self._log("   %s 首帧人数 %d ✓" % (key, n_ff))

        body = {"model": "MiniMax-Hailuo-02", "prompt": prompt,
                "duration": SEG_SECONDS,
                "resolution": VIDEO_FORMATS.get(self.state.get("format"), VIDEO_RESOLUTION),
                "prompt_optimizer": False}
        # Left out, h3 samples from seed 0 every time, so re-rendering a shot
        # reproduces it frame for frame - including whatever went wrong.
        if seed is not None:
            body["seed"] = int(seed)

        self._log("▶ %s %.2f 秒 = %d 段 x %.2f，旁白 %.1f 秒，首帧 %s"
                  % (key, nseg * seg, nseg, seg, voice_secs, note))

        # 2b. The previz for THIS shot, at the length it is ACTUALLY going to
        #     run. The chapter-level pass plans from an estimate of the
        #     narration; only here, after the voice track exists, is the
        #     segment count exact - and a previz one segment short leaves the
        #     tail of the shot with no control at all.
        if PREVIZ_ENABLED:
            try:
                _ensure_previz(self.slug, n, shot, bp, nseg, nseg * seg, fmt,
                               crop=ratio if (ratio is not None and ratio < 1.0) else None)
            except Exception as exc:                        # noqa: BLE001
                self._log("！%s 预演没出来（按无控制渲染）：%s" % (key, str(exc)[:140]))
        if cold and PREVIZ_ENABLED:
            self._log("   %s 冷开场（首帧是场景图），这一镜不送控制视频" % key)

        # 3. Render the segments, each one starting where the last one ended.
        parts, canvas, tail = [], "", None
        self._mark(seg=0, segs=nseg)
        for i in range(nseg):
            t_seg = time.time()
            body["prompt"] = _segment_prompt(prompt, i, nseg, shot, bp, fmt,
                                             voice_secs=voice_secs, seg=seg,
                                             voice_start=float(cues[0]["start"]) if cues else 0.0)
            body["prompt"], _add, _rm = audit_video_prompt(body["prompt"], shot, bp, i)
            if _add or _rm:
                self._log("   %s 第 %d 段提示词校验：%s%s" % (
                    key, i + 1, ("补上 " + "、".join(_add)) if _add else "",
                    ("；删掉 " + "、".join(_rm)) if _rm else ""))
            if note_prose:
                body["prompt"] = note_prose.rstrip("。") + "。" + body["prompt"]
            # The previz for THIS segment, if it was staged. Absent, the body
            # carries no control field and the render is what it always was.
            cv = previz_control(self.slug, n, shot["id"], i, shot=shot) \
                if (PREVIZ_ENABLED and not cold) else ""
            if cv:
                body["control_video"] = "/media/%s/%s" % (
                    self.slug, urllib.parse.quote(
                        os.path.relpath(cv, base).replace(os.sep, "/")))
                body["control_strength"] = PREVIZ_STRENGTH
            else:
                body.pop("control_video", None)
                body.pop("control_strength", None)
            data, q = self._segment(key, body, ref, "%s 第 %d/%d 段" % (key, i + 1, nseg))
            part = os.path.join(work, "%s-seg%d.mp4" % (shot["id"], i))
            with open(part, "wb") as f:
                f.write(data)
            parts.append(part)
            canvas = q.get("canvas") or canvas
            tail = os.path.join(work, "%s-tail.png" % shot["id"])
            _last_frame(part, tail)
            rel = os.path.relpath(tail, base).replace(os.sep, "/")
            ref = "/media/%s/%s" % (self.slug, urllib.parse.quote(rel))
            self._mark(seg=i + 1, minutes=(time.time() - t_seg) / 60.0)
            # The duplicate shows up in the first segment - C01S02 had two men
            # at t=3 s - so ask after the first one rather than after all
            # four. 10 s of vision against the ~100 min the other segments
            # would have cost.
            if i == 0 and nseg > 1 and CLIP_CHECK and want_heads >= 1:
                ok0, seen0 = clip_headcount(part, want_heads)
                if ok0 is False and not CLIP_AUTO_FIX:
                    problems.append("第 1 段数到 %d 个人，应为 %d 个（%s）" % (
                        max(c for _t, c in seen0), want_heads,
                        "、".join("%.1fs:%d" % (t, c) for t, c in seen0)))
                    self._log("⚠ %s 第 1 段人数不对，记为系统建议，继续出完" % key)
                elif ok0 is False:
                    raise RuntimeError(
                        "第 1 段就数到 %d 个人，这一镜只该有 %d 个（%s）"
                        % (max(c for _t, c in seen0), want_heads,
                           "、".join("%.1fs:%d" % (t, c) for t, c in seen0)))
            self._log("   %s %d/%d 段完成（本段 %.0f 分，本镜 %.0f 分）"
                      % (key, i + 1, nseg, (time.time() - t_seg) / 60.0,
                         (time.time() - t0) / 60.0))

        # 4. Join them, then lay the Chinese over h3's invented audio.
        joined = os.path.join(work, "%s-joined.mp4" % shot["id"])
        _concat_stream(parts, joined)
        # Trim the multiple-of-32 padding so the clip leaves here at exactly
        # the requested format - 1920x1088 rendered, 1920x1080 delivered. Done
        # on the picture alone, before the mux, so all three audio branches
        # below keep -c:v copy and the apad reasoning is untouched. Segments
        # and their tail frames stay at the render canvas: the tail is what the
        # next segment starts from and must match what the model drew.
        trimmed = _trim_to_format(
            joined, os.path.join(work, "%s-1080.mp4" % shot["id"]),
            VIDEO_FORMATS.get(self.state.get("format") or "16:9") or VIDEO_RESOLUTION)
        if trimmed != joined:
            got = _dimensions(trimmed)
            if got:
                canvas = "%dx%d" % got
            parts.append(joined)   # scratch, cleared with the segments below
            joined = trimmed
        # 4a. The fight grade (services/fightcut.py): undercrank, the stop and
        #     punch-in on the hit, handheld, crash zoom, whip. Picture only and
        #     before the mux, like the trim, so the mux below still copies it.
        #     A fight is silent by rule, so a faster clock moves nothing but
        #     the foley, which is re-timed below from what this returns. A
        #     failed grade loses the grade, never the shot.
        grade = None
        gplan = fightcut.plan(shot, is_combat(shot), bool(voice), orbit=is_orbit(shot))
        if gplan:
            # Keep what the model drew, before any of this. The grade is the
            # part people keep asking to change (1.4x, 3x, a ramp...), and
            # re-grading a kept source is seconds of CPU where re-rendering is
            # hours of GPU. clips/NN/_raw/<id>.mp4, beside _takes.
            try:
                raw_dir = os.path.join(os.path.dirname(work), "_raw")
                os.makedirs(raw_dir, exist_ok=True)
                shutil.copyfile(joined, os.path.join(raw_dir, shot["id"] + ".mp4"))
            except OSError as exc:
                self._log("！%s 原始画面没存下：%s" % (key, str(exc)[:80]))
            try:
                graded = os.path.join(work, "%s-fight.mp4" % shot["id"])
                grade = fightcut.apply(_ffmpeg(), joined, graded, gplan,
                                       seed=seed or random.randrange(1, 1 << 30))
                parts.append(joined)      # scratch, cleared with the segments
                joined = graded
                self._log("   %s %s：%s%s%s%s%s%s" % (
                    key, "去慢动作" if gplan.get("calm") else "武打剪辑",
                    "快放 %.2f 倍" % grade["speed"] if grade["speed"] > 1 else
                    ("升格原速" if gplan["slow"] else "原速"),
                    "，%.2f 秒处一击（顿帧、推近、震动）" % grade["hit"]
                    if grade["hit"] is not None else ("" if gplan.get("calm") else "，没找到明显的一击"),
                    "，手持" if gplan["hand"] else "", "，急推" if gplan["crash"] else "",
                    "，甩出" if gplan["whip"] else "",
                    "，跟拍横移推近（%.2f 秒）" % (grade["frames"] / grade["fps"])
                    if gplan.get("track") else "",
                    ) + ("，%.2f 秒处升格慢放" % grade["ramp"] if grade.get("ramp") is not None else ""))
            except Exception as exc:                        # noqa: BLE001
                grade = None
                self._log("！%s 武打剪辑没做成（画面照常）：%s" % (key, str(exc)[:110]))
        # 4a2. A VOICED shot ends soon after its line. The picture is rendered
        #      on a 3.75 s grid, so a 2.8 s line got 7.5 s of picture and a 9 s
        #      line 15 s - and h3 keeps the mouth moving through the silence:
        #      tianlongbabu ch01's lip check flagged C01S09/13/14/21/22 at
        #      3.7-14 s, every one of them in the dead tail after the line
        #      (2026-10-06: "voice doesn't match mouth movement"). The sound is
        #      never cut (that rule stands); the PICTURE is, to the line plus a
        #      short handle - and never shorter than the shot's own beats, so a
        #      voiced fight keeps its action. Before the mux, like the trim and
        #      the grade, so the mux still copies the picture.
        if voice and voice_secs > 0 and VOICE_TAIL_TRIM:
            try:
                handle = DIALOG_TAIL_SECONDS if spoken(shot) else VIDEO_HANDLE_SECONDS
                cut = max(voice_secs + handle, VIDEO_MIN_SECONDS, stage.beats_seconds(shot, seg))
                vd = _duration(joined) or 0.0
                if vd - cut >= 0.4:
                    cutp = os.path.join(work, "%s-cut.mp4" % shot["id"])
                    _cut_picture(joined, cutp, cut)
                    parts.append(joined)      # scratch, cleared with the segments
                    joined = cutp
                    self._log("   %s 有声 %.1f 秒，画面在 %.1f 秒处收（原 %.1f 秒，嘴不再对着空气动）"
                              % (key, voice_secs, cut, vd))
            except Exception as exc:                        # noqa: BLE001
                self._log("！%s 画面收尾没裁成（照常）：%s" % (key, str(exc)[:110]))
        final = os.path.join(work, "%s-final.mp4" % shot["id"])
        # Every clip leaves here with exactly one audio stream, in one format,
        # running exactly as long as its picture. Neither used to be true: the
        # silent shots were written with -an and 50 of 103 clips carried no
        # audio stream at all, while a voiced clip's track stopped with the
        # line and left the picture running. Concatenating a chapter with
        # -c copy across that either drops the sound from the first silent shot
        # onward or lets every later clip's audio run ahead of its own image,
        # which is what made the assembled chapter sound wrong rather than just
        # sparse. apad + -shortest pads to the picture; anullsrc gives a silent
        # shot a real track to be concatenated with.
        AR, AC = ["-ar", "48000"], ["-ac", "2"]
        # apad on its own pads forever, and -shortest does not stop it when the
        # video is `-c:v copy`: ffmpeg spun at a fixed file size until _ff's
        # 15-minute timeout, and the shot - four segments, ~two hours of GPU -
        # was thrown away with a truncated "moov atom not found" final. Give
        # apad the picture's own length and it ends by itself (0.15s, measured).
        vdur = _duration(joined)
        pad = "apad=whole_dur=%.3f" % vdur if vdur > 0 else "apad"
        if voice and os.path.isfile(voice):
            if AMBIENT_GAIN > 0 and _has_audio(joined):
                _ff(["-i", joined, "-i", voice, "-filter_complex",
                     "[0:a]volume=%.3f[amb];[amb][1:a]amix=inputs=2:duration=first:"
                     "dropout_transition=0,dynaudnorm=p=0.9[m];[m]%s[a]" % (AMBIENT_GAIN, pad),
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                     "-b:a", "160k"] + AR + AC + ["-shortest", final])
            else:
                # Narration only: h3's own track, if any, is dropped rather
                # than mixed. The picture is sized to outlast the line, so the
                # voice is padded out to it instead of the picture being cut
                # back to the voice.
                _ff(["-i", joined, "-i", voice, "-filter_complex", "[1:a]%s[a]" % pad,
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                     "-b:a", "160k"] + AR + AC + ["-shortest", final])
        else:
            # Nothing said in this shot. h3's invented voice is worse than
            # silence, so it is replaced by real silence - not by no stream at
            # all - and the script's room_tone/ambience/foley go over it in the
            # sound pass.
            _ff(["-i", joined, "-f", "lavfi", "-i",
                 "anullsrc=channel_layout=stereo:sample_rate=48000",
                 "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac",
                 "-b:a", "160k"] + AR + AC + ["-shortest", final])

        # 4b. The foley, on the finished clip. Wrapped for the same reason the
        #     chapter's music mix is: a shot that cost 27 GPU-minutes a segment
        #     must not be thrown away because a sound file was malformed. A
        #     clip with no foley is a clip; a clip that failed to write is two
        #     hours gone.
        if SFX_ENABLED:
            try:
                speed = (grade or {}).get("avg_speed") or (grade or {}).get("speed") or 1.0
                ones, loops = sfx_plan(shot, nseg, seg / speed, _duration(final), bp,
                                       hit=(grade or {}).get("hit"),
                                       cheer=(grade or {}).get("ramp"), grade=grade,
                                       scene=(_read_json(os.path.join(
                                           PROJECTS, self.slug, "script", "%02d.json" % n),
                                           {}) or {}).get("shots"))
                if ones or loops:
                    sfxed = os.path.join(work, "%s-sfx.mp4" % shot["id"])
                    if _mix_sfx(final, sfxed, ones, loops, _duration(final)):
                        parts.append(final)      # scratch, cleared below
                        final = sfxed
                        self._log("   %s 音效 %d 次 + %d 层环境声"
                                  % (key, len(ones), len(loops)))
            except Exception as exc:             # noqa: BLE001
                self._log("！%s 音效没混上（画面照常）：%s" % (key, str(exc)[:120]))

        # 4b2. The caption guard, before the clip is published. Five rounds of
        #      prompt surgery have each been followed by h3 painting something
        #      new; this is the one that does not depend on guessing what it
        #      will write next. Wrapped: a clip with a caption is bad, a clip
        #      lost to a failed crop is two hours of GPU gone.
        if CAPTION_CROP != "off":
            try:
                band = (0.88, 0.95) if CAPTION_CROP == "always" else _caption_band(final)
                if not band and CAPTION_CROP == "auto":
                    # The pixel detector looks for bright glyphs on a dark
                    # outline, and C01S01 (2026-09-29) had white text on a
                    # sunlit floor - no band found, nothing asked, four
                    # captions published. Ask the vision model anyway: two
                    # frames, a few seconds, against two GPU-hours of clip.
                    if _caption_confirm(final, (0.84, 0.96)):
                        band = (0.84, 0.96)
                        self._log("   %s 像素检测没找到字，视觉模型看到了" % key)
                if band:
                    ok = True if CAPTION_CROP == "always" else _caption_confirm(final, band)
                    if ok is None:
                        self._log("   %s 字幕检测：模型没结论，按有字幕处理" % key)
                        ok = True
                    if ok:
                        cropped = os.path.join(work, "%s-nocap.mp4" % shot["id"])
                        _crop_caption(final, cropped, min(CAPTION_KEEP, band[0] - 0.01))
                        parts.append(final)
                        final = cropped
                        self._log("   %s 画面下方有字，已裁掉底部 %.0f%% 并重新构图"
                                  % (key, (1 - min(CAPTION_KEEP, band[0] - 0.01)) * 100))
                        remarks.append("画面下方有字，已裁掉底部 %.0f%%" % ((1 - min(CAPTION_KEEP, band[0] - 0.01)) * 100))
                    else:
                        self._log("   %s 下方那条亮痕不是字，原样保留" % key)
            except Exception as exc:             # noqa: BLE001
                self._log("！%s 字幕裁剪没做成（画面照常）：%s" % (key, str(exc)[:110]))

        # 4b3. Count the people. On the PUBLISHED framing, because the caption
        #      crop changes what is in frame, and before the take is written,
        #      so a clip with two 沈墨 in it never reaches the disk, never
        #      becomes the next shot's first frame, and is re-rendered with a
        #      fresh seed by the retry path.
        if CLIP_CHECK and want_heads >= 1:
            try:
                ok_heads, seen = clip_headcount(final, want_heads)
            except Exception as exc:                        # noqa: BLE001
                ok_heads, seen = None, []
                self._log("！%s 人数没核成（按通过处理）：%s" % (key, str(exc)[:110]))
            shown = "、".join("%.1fs:%d" % (t, c) for t, c in seen)
            if ok_heads is False and not CLIP_AUTO_FIX:
                problems.append("画面里数到 %d 个人，这一镜只该有 %d 个（%s）"
                                % (max(c for _t, c in seen), want_heads, shown))
                self._log("⚠ %s 人数不对，记为系统建议（不自动重出）" % key)
            elif ok_heads is False:
                self._drop_first_frame(shot, cold)
                raise RuntimeError("画面里数到 %d 个人，这一镜只该有 %d 个（%s）"
                                   % (max(c for _t, c in seen), want_heads, shown))
            if ok_heads is None:
                self._log("   %s 人数核对：模型没结论，放行" % key)
            else:
                self._log("   %s 人数 %d ✓（%s）" % (key, want_heads, shown))
            fw = len(fighters(shot, bp))
            if fw:
                try:
                    okf, seenf = clip_fighters(final, fw)
                except Exception as exc:                    # noqa: BLE001
                    okf, seenf = None, []
                    self._log("！%s 打斗人数没核成（按通过处理）：%s" % (key, str(exc)[:110]))
                shownf = "、".join("%.1fs:%d" % (t, c) for t, c in seenf)
                if okf is False:
                    msg = "有 %d 个人在打，这一镜只该 %d 个人交手（%s）" % (
                        max(c for _t, c in seenf), fw, shownf)
                    if CLIP_AUTO_FIX:
                        self._drop_first_frame(shot, cold)
                        raise RuntimeError(msg)
                    problems.append(msg)
                elif okf is True:
                    self._log("   %s 交手 %d 人 ✓（%s）" % (key, fw, shownf))
        # The shot review, picture half: scene, people, clothes, action.
        if CLIP_CHECK:
            try:
                _dur = _duration(final) or 0.0
                ask = lambda q, at, pre, temp: _vlm_json(q, _grab(final, at), prefill=pre,
                                                         temperature=temp)
                review = shotqa.check_picture(shot, bp, ask, _dur, is_group=is_group)
            except Exception as exc:                        # noqa: BLE001
                review = []
                self._log("！%s 镜头核对没做成（按通过处理）：%s" % (key, str(exc)[:110]))
            hard = [t for h, t in review if h]
            for h, t in review:
                self._log("%s %s %s" % ("✗" if h else "⚠", key, t))
                (problems if h else remarks).append(t)
            if hard and CLIP_AUTO_FIX:
                self._drop_first_frame(shot, cold)
                raise RuntimeError("镜头核对没过：" + "；".join(hard))
            if not review:
                self._log("   %s 镜头核对 ✓（场景、人物、动作）" % key)

        with open(final, "rb") as f:
            data = f.read()
        rel = "clips/%02d/%s.mp4" % (n, shot["id"])
        full = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        secs = _duration(final)
        # The ceiling bound and the line did not fit. Say so by name - this is
        # the one case left where a sentence is cut, and it must never be
        # silent again - and keep the subtitles honest about it.
        if voice_secs > secs + 0.05:
            kept = _clamp_cues(cues, secs)
            self._log("！%s 旁白 %.1f 秒装不进 %.1f 秒画面，末尾 %.1f 秒没有出声；"
                      "字幕已裁到画面长度（%d/%d 条）"
                      % (key, voice_secs, secs, voice_secs - secs, len(kept), len(cues)))
            cues = kept
        take = _take_add(full, data, {
            "seconds": round(secs, 2), "resolution": canvas or body["resolution"],
            "segments": nseg, "voice_seconds": round(voice_secs, 2),
            "first_frame": note, "elapsed": int(time.time() - t0), "cues": cues,
            "user_note": user_note or None,
            "check": {"ok": not problems, "problems": problems, "remarks": remarks}})
        self._log("✓ %s 第 %d 次，%.1f 秒，%d 段，%d 分，%.1f MB%s"
                  % (key, take, secs, nseg, (time.time() - t0) / 60.0, len(data) / 1e6,
                     "，有中文旁白" if voice else "，无台词"))

        # 4c. The lip check, on the finished clip, the way every reference
        #     image is verified the moment it lands. Three frames out of this
        #     shot's SILENT windows, one question each to the vision model:
        #     about 100 s against the ~80 GPU-minutes the shot just cost, so it
        #     is affordable where a second sampling pass is not. Wrapped, for
        #     the same reason the foley mix is: a verdict is a thing to read,
        #     never a reason to lose two hours of picture.
        if LIPCHECK_AUTO:
            try:
                lip = lipcheck_shot(self.slug, n, shot)
                if lip:
                    lip["id"] = shot["id"]
                    lip["at"] = int(time.time())
                    out = lip_path(self.slug, n, shot["id"])
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "w", encoding="utf-8") as fh:
                        json.dump(lip, fh, ensure_ascii=False)
                    if lip.get("ok") is False:
                        self._log("⚠ %s 口型：%s" % (key, lip.get("note") or ""))
                    elif lip.get("ok") is None:
                        self._log("   %s 口型：没有结论（%s）" % (key, lip.get("note") or ""))
            except Exception as exc:             # noqa: BLE001
                self._log("   %s 口型没查成（画面照常）：%s" % (key, str(exc)[:110]))

        # 4d. And onto the lip-sync queue, if it speaks and the model is here.
        if LIPSYNC_AUTO and spoken(shot) and _lipsync_py():
            try:
                r = lipsync_batch_for(self.slug).start([key])
                self._log("   %s 有台词，排入口型同步（队列 %d）" % (key, r.get("queued", 0)))
            except Exception as exc:             # noqa: BLE001
                self._log("   %s 排口型同步失败：%s" % (key, str(exc)[:110]))

        # 5. Hand the tail to the next shot and clear the segment scratch.
        self._prev = {"key": key, "shot": shot, "png": tail} if tail else None
        # A shot re-rendered after the one that FOLLOWS it: that one opened on
        # (or was drawn from) the end of the version just replaced. Say so -
        # redrawing it is the person's call, not the queue's.
        try:
            sc = _read_json(os.path.join(base, "script", "%02d.json" % n), {}) or {}
            ids = [x.get("id") for x in sc.get("shots") or []]
            nxt = ids[ids.index(shot["id"]) + 1] if shot["id"] in ids[:-1] else None
            nk = "%d:%s" % (n, nxt) if nxt else None
            if nk and nk not in (self.state.get("queue") or []) and os.path.isfile(
                    os.path.join(base, "clips", "%02d" % n, "%s.mp4" % nxt)):
                self._log("⚠ %s 是按 %s 的旧版末尾接的，%s 已重出；要接上新版，请重出 %s"
                          % (nk, key, key, nk))
        except Exception:                                   # noqa: BLE001
            pass
        for q in parts + [joined]:
            try:
                if q != final:
                    os.unlink(q)
            except OSError:
                pass
        return


_VIDEO = {}


# ----------------------------------------------------------------------
# 4K pass. Deliberately a SEPARATE queue from VideoBatch, not a step inside
# _render: the GAN and the sampler both want the whole GPU, and welding them
# together would mean a shot's wall clock is render + upscale and a failed
# upscale stalls the render queue behind it. Kept apart, the native clip stays
# the source of truth, this pass is re-runnable when the model or the target
# changes, and it can be left to run overnight after a chapter.
UPSCALE_MODEL = os.environ.get(
    "BOOKREEL_UPSCALE_MODEL",
    os.path.join(ROOT, "ComfyUI", "models", "upscale_models", "RealESRGAN_x4plus.pth"))
UPSCALE_TARGET = os.environ.get("BOOKREEL_UPSCALE_TARGET", "3840x2160")
# Measured 2026-09-11 on this box: 2.42 s/frame for 1312x736 -> x4 -> 3840x2160,
# GPU pinned at 97-99% the whole time, so it is the GAN that is the wall and
# not the x264 encode riding along on the CPU. A 15 s shot is 360 frames, so
# ~15 min; a shot may legitimately take a while, but not forever.
UPSCALE_STALL = int(os.environ.get("BOOKREEL_UPSCALE_STALL", str(90 * 60)))
UPSCALE_CRF = os.environ.get("BOOKREEL_UPSCALE_CRF", "16")


def _upscale_py():
    """The worker runs in the COMFY venv - it is the one with torch and
    spandrel. web_server itself is stdlib-only and must stay that way."""
    for c in (os.path.join(ROOT, "venvs", "comfy", "bin", "python"),
              os.path.join(ROOT, "venvs", "comfy", "Scripts", "python.exe")):
        if os.path.isfile(c):
            return c
    return None


class UpscaleBatch:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "upscale_batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.proc = None
        self.state = _read_json(self.path, None) or {
            "status": "idle", "queue": [], "done": [], "failed": []}

    _save = VideoBatch._save
    snapshot = VideoBatch.snapshot
    _log = VideoBatch._log

    def paths(self, key):
        """(native clip, 4K clip) for a shot key. The 4K tree mirrors clips/
        exactly, so nothing has to be renamed when a chapter is assembled."""
        n, sid = key.split(":", 1)
        base = os.path.join(PROJECTS, self.slug)
        return (os.path.join(base, "clips", "%02d" % int(n), sid + ".mp4"),
                os.path.join(base, "clips4k", "%02d" % int(n), sid + ".mp4"))

    def start(self, keys, front=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                have = set(self.state["queue"]) | {self.state.get("current") or ""}
                fresh = [k for k in keys if k not in have]
                if front:
                    self.state["queue"][0:0] = fresh
                else:
                    self.state["queue"].extend(fresh)
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"])}
            self.state = {"status": "running", "queue": list(keys), "done": [], "failed": [],
                          "started": int(time.time()), "current": None, "current_started": None,
                          "last_error": "", "log": []}
            self._save()
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="4k-" + self.slug, daemon=True)
            self.thread.start()
            return {"ok": True, "queued": len(keys)}

    def resume(self):
        st = self.state
        if st.get("status") == "running" and (st.get("queue") or st.get("current")):
            if st.get("current") and st["current"] not in st["queue"]:
                st["queue"].insert(0, st["current"])
            st["current"] = None
            self._log("服务重启，续跑 4K：剩余 %d 镜" % len(st["queue"]))
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="4k-" + self.slug, daemon=True)
            self.thread.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        self._log("收到停止：当前这镜放大完就停")
        # Unlike a render, killing this costs one clip's worth of GPU and the
        # partial file is worthless, so the running worker goes down with it.
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def _run(self):
        while True:
            with self.lock:
                if self.stop_flag or not self.state["queue"]:
                    self.state["status"] = "stopped" if self.stop_flag else (
                        "done" if not self.state["failed"] else "done_with_failures")
                    self.state["current"] = None
                    self.state["finished"] = int(time.time())
                    self._save()
                    return
                key = self.state["queue"].pop(0)
                self.state["current"] = key
                self.state["current_started"] = int(time.time())
                self._save()
            try:
                self._one(key)
                with self.lock:
                    self.state["done"].append(key)
                    self._save()
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["failed"].append(key)
                    self.state["last_error"] = str(exc)[:300]
                    self._save()
                self._log("！%s 4K 失败：%s" % (key, str(exc)[:140]))

    def _one(self, key):
        src, dst = self.paths(key)
        if not os.path.isfile(src):
            raise RuntimeError("还没有这镜的成片")
        # Resumable and idempotent: a 4K file newer than its source is already
        # this clip's, so a re-run picks up where it stopped. A re-rendered
        # take makes the source newer and the 4K is redone.
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            self._log("· %s 已有 4K，跳过" % key)
            return
        py = _upscale_py()
        if not py:
            raise RuntimeError("找不到 venvs/comfy 的 python")
        if not os.path.isfile(UPSCALE_MODEL):
            raise RuntimeError("没有放大模型 %s" % os.path.basename(UPSCALE_MODEL))
        tw, _, th = UPSCALE_TARGET.partition("x")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # ".part.mp4", not ".mp4.part": ffmpeg picks the muxer off the
        # extension and refused to initialise one for a .part file.
        tmp = dst + ".part.mp4"
        t0 = time.time()
        self._log("▶ %s 放大到 %s" % (key, UPSCALE_TARGET))
        cmd = [py, os.path.join(ROOT, "services", "upscale.py"),
               "--in", src, "--out", tmp, "--model", UPSCALE_MODEL,
               "--width", tw.strip(), "--height", th.strip(), "--crf", UPSCALE_CRF]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.proc = p
        tail = []
        try:
            for line in iter(p.stdout.readline, b""):
                line = line.decode("utf-8", "replace").rstrip()
                if line:
                    tail.append(line)
                    del tail[:-12]
                if time.time() - t0 > UPSCALE_STALL:
                    p.kill()
                    raise RuntimeError("超过 %d 分钟没出片" % (UPSCALE_STALL // 60))
            rc = p.wait(timeout=300)
        finally:
            self.proc = None
            try:
                p.stdout.close()
            except OSError:
                pass
        if self.stop_flag and rc != 0:
            raise RuntimeError("已停止")
        if rc != 0 or not os.path.isfile(tmp):
            raise RuntimeError("放大失败：%s" % (" / ".join(tail[-3:]) or "rc=%s" % rc))
        os.replace(tmp, dst)
        got = _dimensions(dst) or (0, 0)
        self._log("✓ %s 4K %dx%d，%.1f 分，%.0f MB"
                  % (key, got[0], got[1], (time.time() - t0) / 60.0,
                     os.path.getsize(dst) / 1e6))


# ----------------------------------------------------------------------
# Scene music. One instrumental bed per LOCATION, not per shot: a cue that
# restarts every ten seconds is not scoring, it is a stutter, and the script
# already writes music as something that enters, holds and leaves across a run
# of shots in one place (sound.music.cue = 进 / 延续 / 出).
#
# A THIRD queue, for the same reason the 4K pass is its own: ACE-Step wants the
# whole GPU and so does the video sampler. Welded into the render, a shot's
# wall clock becomes picture + music. Apart, music can be generated before the
# render, after it, or on a second machine, and a failed bed never stalls a
# clip.
MUSIC_WORKFLOW = os.path.join(ROOT, "comfy-ace-music.json")
# A bed is as long as the picture it has to cover, and no shorter. It used to
# be a flat 60 s laid under every span with -stream_loop -1, so a location
# with 200 s of music heard the same minute three and a half times, and the
# six separate entrances chapter 1 asks for were six plays of one recording.
# Looping a bed is not scoring either - it is the same stutter as a cue that
# restarts every ten seconds, just on a longer period.
#
# So the length is planned from the script (music_seconds) and each entrance
# takes its OWN slice, continuing where the last one at that location stopped.
# The music picks up where it left off, which is what a composer would do
# with a returning theme anyway.
MUSIC_SECONDS = float(os.environ.get("BOOKREEL_MUSIC_SECONDS", "60"))
# 240 s is where ACE-Step stops composing and starts droning, measured on
# xuemang lo01 (2026-09-17) with the envelope's own standard deviation over
# 250 ms frames - how much the music actually moves:
#
#     240 s bed   first60 std 8.9   mid 8.1   last60 5.3
#     340 s bed   first60 std 4.3   mid 0.8   last60 0.8
#
# The 340 s take is not a loop - its tail correlates 0.14 with anything
# earlier, so it IS fresh material - it is just flat: a held drone rather than
# a piece. Cheap either way (101 s of GPU for 240 s of music, 146 s for 340),
# so the ceiling is musical, not economic. A location needing more than this
# hears one restart, and _mix_music says which one by name.
MUSIC_MAX_SECONDS = float(os.environ.get("BOOKREEL_MUSIC_MAX_SECONDS", "240"))
# What the planner cannot know: a shot's real clip runs longer than the
# script's `seconds` as often as not, because the picture is sized to the
# narration. Buy the difference here rather than discover it at mix time.
MUSIC_HEADROOM = 1.25
MUSIC_STEPS = int(os.environ.get("BOOKREEL_MUSIC_STEPS", "50"))
MUSIC_STALL = int(os.environ.get("BOOKREEL_MUSIC_STALL", str(60 * 60)))
# The negative side of the guidance, and the ONLY place a voice may be named.
# ACE-Step will sing over a bed unless it is told not to here: it is a song
# model first and an underscore model second. Put in every shape a voice takes
# in its training tags, in both languages, because a bed with a vocal line in
# it fights the narration for the same frequencies and cannot be used at all.
MUSIC_NEGATIVE = os.environ.get(
    "BOOKREEL_MUSIC_NEGATIVE",
    "vocals, vocal, singing, singer, voice, human voice, choir, chorus, "
    "lyrics, rap, humming, chant, a cappella, spoken word, "
    "人声, 演唱, 歌声, 合唱, 说唱, 吟唱, "
    # What it falls back on when the palette does not reach it: a western
    # film-library cue. Subtracted rather than merely left unsaid, because
    # unlike Flux and h3 this model's negative branch is a real CFG.
    "symphony orchestra, orchestral strings, film score orchestra, piano, "
    "drum kit, electric guitar, synthesizer, electronic beat")


def music_dir(slug):
    return os.path.join(PROJECTS, slug, "assets", "music")


def _music_tags(slug, loc_id):
    """The ACE-Step tag line for one location, built in code from the script.

    ACE-Step takes a comma-separated list of style words, not a sentence, so
    this is assembled rather than written by the LLM - the same reason
    _anchor() builds the image prompts. Everything in it is written in the
    English tag vocabulary ACE-Step was trained on, because that is the only
    vocabulary it has: genre and instruments first (look.music), then the two
    instruments the shots themselves asked for, then their mood, then what the
    bed is FOR. "traditional Chinese classical music, guqin zither" in front
    produces a Chinese period bed; the same intent written 古琴，箫 produced
    library orchestral for every bed of chapter 1.
    """
    base = os.path.join(PROJECTS, slug)
    bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
    moods, instruments = [], []
    label = loc_id
    for lo in (bp.get("locations") or []):
        if str(lo.get("id") or "") == loc_id:
            label = lo.get("name") or loc_id
            break
    sdir = os.path.join(base, "script")
    if os.path.isdir(sdir):
        for f in sorted(os.listdir(sdir)):
            if not re.match(r"^\d\d\.json$", f):
                continue
            for sh in (_read_json(os.path.join(sdir, f), {}) or {}).get("shots", []):
                if str(sh.get("location_id") or "") != loc_id:
                    continue
                m = (sh.get("sound") or {}).get("music") or {}
                if str(m.get("cue") or "").strip() in ("无", ""):
                    continue
                for src, dst in ((m.get("mood"), moods), (m.get("instrument"), instruments)):
                    v = str(src or "").strip()
                    if v and v not in dst:
                        dst.append(v)
    world = bp.get("world") or {}
    # "instrumental" is a positive word and belongs here. "no vocals" does NOT:
    # naming the thing you do not want puts it in, exactly as with the image
    # prompts (CLAUDE.md). The first bed generated with "no vocals" in these
    # tags came back SINGING - SenseVoice transcribed Korean lyrics off it.
    # What excludes a voice is the negative conditioning, which is subtracted
    # rather than read; see MUSIC_NEGATIVE.
    bits = ["instrumental"]
    # The world's own instruments FIRST, ahead of era and place: ACE-Step
    # weights its opening tokens, and 古琴/竹笛 at the end gave library
    # orchestral with a flute somewhere in it. Derived from the blueprint
    # (look.music) rather than hardcoded, so a story set somewhere else is
    # played on that somewhere else's instruments.
    native, smooth = look.music(bp)
    bits.extend(native[:5])
    # What the script asked for, minus anything that does not belong in this
    # world - a Ming chapter that writes 弦乐 or 钢琴 into sound.music gets a
    # concert hall under a Suzhou courtyard.
    # …and only the ones that name an instrument. The script writes this field
    # as a direction ("竹笛长音持续", "低音笙长音渐弱消失") as often as an
    # instrument, and a sentence in a tag list is weight spent on nothing. One
    # that merely re-says an instrument already named above is dropped whole;
    # what is left has to be short enough to be a noun.
    # …and translated: the field is Chinese and this model's tags are not
    # (look.instrument_tag). A word it cannot place - which is most of them,
    # because the field is a direction as often as an instrument - is dropped.
    asked = []
    for i in instruments:
        t = look.instrument_tag(i, bp)
        if t and t not in bits and t not in asked:
            asked.append(t)
    bits.extend(asked[:2])
    if not native and not asked:
        bits.extend(["traditional Chinese classical music", "guqin zither",
                     "dizi bamboo flute"])
    # No era and no place. They were the blueprint's own Chinese prose -
    # 晚明·崇祯六年（1633）, 江南·苏州府及运河沿线 - and a date with brackets
    # and a place name with a 、 in it is not a tag this model has ever seen;
    # it is four hundred characters of weight spent on nothing, in front of
    # the words that do work. The genre tag carries the period.
    bits.extend(look.mood_tags(moods)[:3] or ["quiet", "restrained"])
    # A bed sits UNDER narration, so say so: left to itself the model writes a
    # foreground piece with a melody and a beat that fight the voice. Slow and
    # smooth are stated every time, for every setting.
    bits.extend(smooth)
    # ASCII commas: this is a tag list in the model's own format, and a
    # full-width comma is not a separator to it.
    return ", ".join(b for b in bits if b)[:400], label


def music_seconds(slug, loc_id):
    """How long this location's bed has to be so no span ever repeats it.

    The sum of that location's spans within ONE chapter, whichever chapter
    asks for the most, because each film restarts the bed at its beginning.
    Across chapters the same recording returning IS scoring - a location
    theme - and nobody hears two chapters back to back the way they hear two
    entrances ten minutes apart.
    """
    base = os.path.join(PROJECTS, slug)
    sdir = os.path.join(base, "script")
    need = 0.0
    if os.path.isdir(sdir):
        for f in sorted(os.listdir(sdir)):
            m = re.match(r"^(\d\d)\.json$", f)
            if not m:
                continue
            got = sum(e - s for lid, s, e in music_spans(slug, int(m.group(1)), planned=True)
                      if lid == loc_id)
            need = max(need, got)
    if need <= 0:
        return MUSIC_SECONDS
    want = need * MUSIC_HEADROOM + FILM_MUSIC_FADE * 2
    # ACE-Step takes a float; round up to a whole ten so a re-plan after a
    # small script edit does not make every bed stale.
    return float(max(MUSIC_SECONDS, min(MUSIC_MAX_SECONDS, math.ceil(want / 10.0) * 10)))


def music_items(slug):
    """One row per location that any shot actually uses, with its bed's state."""
    base = os.path.join(PROJECTS, slug)
    used, order = {}, []
    sdir = os.path.join(base, "script")
    if os.path.isdir(sdir):
        for f in sorted(os.listdir(sdir)):
            if not re.match(r"^\d\d\.json$", f):
                continue
            for sh in (_read_json(os.path.join(sdir, f), {}) or {}).get("shots", []):
                lid = str(sh.get("location_id") or "").strip()
                if not lid:
                    continue
                if lid not in used:
                    used[lid] = 0
                    order.append(lid)
                used[lid] += 1
    rows = []
    for lid in order:
        tags, label = _music_tags(slug, lid)
        f = os.path.join(music_dir(slug), lid + ".mp3")
        ex = os.path.isfile(f)
        want = music_seconds(slug, lid)
        got = round(_duration(f), 1) if ex else 0
        rows.append({"id": lid, "label": label, "shots": used[lid], "tags": tags,
                     "exists": ex,
                     # A bed generated before the plan grew is not wrong, it is
                     # SHORT - and a short bed is the one that has to repeat.
                     "want": want, "short": bool(ex and got < want - 5),
                     "seconds": got,
                     "url": ("/media/%s/assets/music/%s.mp3?t=%d"
                             % (slug, lid, int(os.path.getmtime(f)))) if ex else ""})
    return rows


class MusicBatch:
    """Generate scene beds, one at a time, in the server. Same shape as the
    other two queues: state on disk, resumes after a restart, stops between
    items, and retries rather than dying when ComfyUI is away."""

    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "music_batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.state = _read_json(self.path, None) or {
            "status": "idle", "queue": [], "done": [], "failed": []}

    _save = VideoBatch._save
    snapshot = VideoBatch.snapshot
    _log = VideoBatch._log
    _comfy = Batch._comfy

    def start(self, ids, redo=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                have = set(self.state["queue"]) | {self.state.get("current") or ""}
                self.state["queue"].extend([i for i in ids if i not in have])
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"])}
            self.state = {"status": "running", "queue": list(ids), "done": [], "failed": [],
                          "started": int(time.time()), "current": None, "current_started": None,
                          "redo": bool(redo), "last_error": "", "log": []}
            self._save()
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="music-" + self.slug, daemon=True)
            self.thread.start()
            return {"ok": True, "queued": len(ids)}

    def resume(self):
        st = self.state
        if st.get("status") == "running" and (st.get("queue") or st.get("current")):
            ids = ([st["current"]] if st.get("current") else []) + list(st.get("queue") or [])
            return self.start(ids, st.get("redo"))
        return None

    def stop(self):
        self.stop_flag = True
        with self.lock:
            self.state["status"] = "stopping"
            self._save()

    def _run(self):
        while True:
            with self.lock:
                if self.stop_flag or not self.state["queue"]:
                    self.state["status"] = "stopped" if self.stop_flag else "done"
                    self.state["current"] = None
                    self._save()
                    return
                key = self.state["queue"].pop(0)
                self.state["current"] = key
                self.state["current_started"] = int(time.time())
                self._save()
            try:
                self._one(key)
                with self.lock:
                    self.state["done"].append(key)
            except Exception as exc:                        # noqa: BLE001
                self._log("× %s：%s" % (key, str(exc)[:160]))
                with self.lock:
                    self.state["failed"].append(key)
            with self.lock:
                self.state["current"] = None
                self._save()

    def _one(self, loc_id):
        wf = _read_json(MUSIC_WORKFLOW, None)
        if not wf:
            raise RuntimeError("读不到配乐工作流 %s" % MUSIC_WORKFLOW)
        wf.pop("_comment", None)
        tags, label = _music_tags(self.slug, loc_id)
        # As long as this location's picture, so no entrance ever replays a
        # minute the audience has already heard. See music_seconds.
        secs = music_seconds(self.slug, loc_id)
        wf["2"]["inputs"]["tags"] = tags
        wf["3"]["inputs"]["tags"] = MUSIC_NEGATIVE
        wf["4"]["inputs"]["seconds"] = secs
        wf["5"]["inputs"]["seed"] = random.getrandbits(32)
        wf["5"]["inputs"]["steps"] = MUSIC_STEPS
        wf["7"]["inputs"]["filename_prefix"] = "bookreel/music-%s-%s" % (self.slug, loc_id)
        if "__TAGS__" in json.dumps(wf, ensure_ascii=False):
            raise RuntimeError("工作流里残留 __TAGS__ 占位符")
        self._log("▶ %s（%s）%.0f 秒：%s" % (loc_id, label, secs, tags[:90]))

        pid = None
        while pid is None:
            if self.stop_flag:
                raise RuntimeError("停止")
            try:
                pid = self._comfy("/prompt", {"prompt": wf, "client_id": BATCH_CLIENT})["prompt_id"]
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["last_error"] = "ComfyUI 不可达，30 秒后重试：%s" % str(exc)[:120]
                    self._save()
                time.sleep(30)
        with self.lock:
            self.state["last_error"] = ""
            self._save()

        t0 = time.time()
        while True:
            time.sleep(5)
            if self.stop_flag:
                raise RuntimeError("停止")
            if time.time() - t0 > MUSIC_STALL:
                raise RuntimeError("%s 等了 %d 分钟没有结果" % (loc_id, MUSIC_STALL // 60))
            try:
                rec = self._comfy("/history/" + pid, timeout=30).get(pid)
            except Exception:                               # noqa: BLE001
                continue
            if not rec:
                continue
            stx = rec.get("status", {})
            if stx.get("status_str") == "error":
                raise RuntimeError("ComfyUI 任务失败：%s"
                                   % json.dumps(stx.get("messages", [])[-1:], ensure_ascii=False)[:200])
            # An audio node reports under "audio", not "images".
            for out in rec.get("outputs", {}).values():
                for au in (out.get("audio") or []):
                    url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                        COMFY, urllib.parse.quote(au["filename"]),
                        urllib.parse.quote(au.get("subfolder", "")), au.get("type", "output"))
                    with urllib.request.urlopen(url, timeout=300) as r:
                        body = r.read()
                    if not body:
                        raise RuntimeError("取到 0 字节")
                    os.makedirs(music_dir(self.slug), exist_ok=True)
                    dst = os.path.join(music_dir(self.slug), loc_id + ".mp3")
                    tmp = dst + ".part"
                    with open(tmp, "wb") as f:
                        f.write(body)
                    os.replace(tmp, dst)
                    self._log("✓ %s %.1f 秒，%.1f MB，%d 分"
                              % (loc_id, _duration(dst), len(body) / 1e6, (time.time() - t0) / 60))
                    return
            if rec.get("outputs"):
                raise RuntimeError("任务完成但没有音频输出")


# ----------------------------------------------------------------------
# caption guard - the last resort, and the only one that cannot be argued with
# ----------------------------------------------------------------------
# Five rounds of prompt surgery have each removed a real source of burned-in
# text and each been followed by h3 painting something else: the era anchor,
# the anatomy list, the camera labels, the same block rewritten as prose, the
# same block rewritten as plain viewer description, the segment's own phase
# words. The prompt is now free of every statement about the picture - and
# that is the right fix, because it is the only one that keeps the whole
# frame. But it is a fix by absence, and absence cannot be verified in
# advance.
#
# This is the one that can. Measured across every captioned clip in the
# project, the text sits in the SAME PLACE: 88-95% of the frame height,
# centred. C01S01 2026-09-19: rows 90.8-95.1%. So cropping the bottom tenth
# and re-framing to the same canvas removes it mechanically, for any wording,
# in any language, whatever the model decides to write next.
#
# The cost is real and is why this is not simply always on: an 11% punch-in,
# losing the bottom of the frame and the same fraction of the sides to keep
# 16:9. `auto` pays it only on a clip that has text in that band; `always`
# pays it everywhere and never has to be right about detection; `off` is the
# old behaviour.
CAPTION_CROP = os.environ.get("BOOKREEL_CAPTION_CROP", "auto")   # auto | always | off
CAPTION_KEEP = float(os.environ.get("BOOKREEL_CAPTION_KEEP", "0.88"))


def _caption_band(path, fps=2):
    """(lo, hi) as fractions of height where bright-on-dark text sits, or None.

    Bright glyphs over a dark outline, in the same rows, across many frames -
    which is what a burned-in subtitle is and what a lit object is not. Cheap:
    one ffmpeg decode at 656x368, no model.
    """
    exe = _ffmpeg()
    if not exe or not os.path.isfile(path):
        return None
    w, h = 656, 368
    pr = subprocess.run([exe, "-v", "error", "-i", path,
                         "-vf", "fps=%d,scale=%d:%d" % (fps, w, h),
                         "-pix_fmt", "gray", "-f", "rawvideo", "-"],
                        capture_output=True, timeout=300)
    buf = pr.stdout
    n = len(buf) // (w * h)
    if n < 2:
        return None
    try:
        import array
        rows = [0.0] * h
        frames = 0
        for f in range(n):
            base = f * w * h
            a = array.array("B")
            a.frombytes(buf[base:base + w * h])
            frames += 1
            for y in range(int(h * 0.6), h):
                off = y * w
                hit = 0
                for x in range(3, w):
                    if a[off + x] > 225 and a[off + x - 3] < 70:
                        hit += 1
                rows[y] += hit / float(w)
        rows = [r / max(1, frames) for r in rows]
    except Exception:                                       # noqa: BLE001
        return None
    lower = rows[int(h * 0.6):]
    if not lower:
        return None
    floor = max(0.004, 6.0 * (sum(rows) / len(rows)))
    hits = [int(h * 0.6) + i for i, v in enumerate(lower) if v > floor]
    if not hits:
        return None
    return (min(hits) / float(h), max(hits) / float(h))


def _caption_confirm(path, band):
    """Ask the verifier whether that band really is text. None = no opinion."""
    lo = max(0.0, band[0] - 0.04)
    votes = []
    # Four looks, the last one near the END: h3 writes a later segment's beat
    # late in the clip, and C01S21/22 (2026-10-06) carried 说到侥幸后停顿半拍 /
    # 头微仰长随哈哈晃 in their last three seconds, where two looks at 35% and
    # 65% never saw them.
    dur = _duration(path) or 1.0
    for t in (0.22, 0.47, 0.72, 0.93):
        try:
            png = _grab(path, dur * t)
            ans = _vlm_json(
                "看这一帧的下半部分。画面上有没有叠加上去的字幕或印刷体文字"
                "（不是物件本身的字，比如书上的字、招牌）？"
                '只输出 {"字幕":"有"} 或 {"字幕":"没有"}。', png, prefill='{"字幕":')
            v = str((ans or {}).get("字幕", "")).strip()
            if "有" in v and "没有" not in v:
                votes.append(True)
            elif v:
                votes.append(False)
        except Exception:                                   # noqa: BLE001
            return None
    if not votes:
        return None
    return any(votes)


# ----------------------------------------------------------------------
# the head count -- the one defect that makes a clip unusable and that no
# prompt has ever reliably prevented
# ----------------------------------------------------------------------
# h3 draws its single subject twice often enough to be the rule rather than
# luck: 沈墨 at one desk in C01S02 (twice now, 2026-09-16 and 2026-09-20),
# 阿蘅 side by side in C01S08, a second man invented in the alley behind
# C01S01. Every lever in CLAUDE.md has been aimed at it - the count said out
# loud, said early, restated as a fact about the place rather than the frame,
# and the control video - and it still happens, because it is a sampling
# outcome and not a prompt bug. A sampling outcome has one honest answer:
# LOOK at what came back, and roll again. The retry path already re-rolls the
# seed.
#
# The image verifier's whole discipline applies, for a bigger stake - a
# re-render here is ~1.8 GPU-hours, not seven minutes. No verdict is never a
# failure, and an overcount is believed only when the same frame says it
# twice.
CLIP_CHECK = os.environ.get("BOOKREEL_CLIP_CHECK", "1") not in ("0", "", "false")
CLIP_CHECK_AT = (0.3, 0.55, 0.8, 0.95)


def cast_ids(shot, bp):
    """The shot's cast, as ids the blueprint actually knows."""
    by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or []) if c.get("id")}
    return [str(i) for i in ((shot or {}).get("characters") or []) if str(i) in by_id]


# A "character" that is really a CROWD: the 分镜 lists 无量剑宗弟子 and
# 女弟子们 in a hall shot the way it lists a person. Counted as one, the clip
# check expects six people in a hall of forty and fails the shot three times
# over (6+ GPU-hours) - tianlongbabu C01S01/S05/S14, caught before rendering.
_GROUP_RX = re.compile(r"(们|众|群|诸|宾客|帮众|弟子|门人|侍卫|百姓|兵丁|士卒)$")
CROWD = 99


def _age_words(age):
    """"25" -> "25岁", "十九" -> "十九岁", "五十余（…）" -> "五十余岁", "中年" -> "中年".

    Stripping everything but digits was fine while blueprints wrote 25; an
    imported novel's blueprint writes ages the way the book does, and every
    one of them came out as an ageless 一个男子.
    """
    a = _plain(age) or ""
    m = re.match(r"\s*([\d零一二两三四五六七八九十百]+)\s*(余|多|来)?", a)
    if m:
        return m.group(1) + (m.group(2) or "") + "岁"
    m = re.search(r"(少年|青年|中年|老年|年轻|年老|幼)", a)
    return {"年轻": "青年", "年老": "老年"}.get(m.group(1), m.group(1)) if m else ""


def is_group(c):
    return bool(c) and (bool(c.get("group")) or bool(_GROUP_RX.search(_plain(c.get("name")) or "")))


def cast_count(shot, bp):
    """How many people the prompt tells h3 are there - and therefore how many
    the finished picture may hold. `_people_clause` says "这里只有一个人" off
    the same list, so the claim and the check are one number. A crowd in the
    cast lifts the ceiling: nobody can say how many a crowd is."""
    by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or []) if c.get("id")}
    ids = cast_ids(shot, bp)
    if any(is_group(by_id.get(i)) for i in ids):
        return CROWD
    return len(ids)


def fighters(shot, bp):
    """Who is FIGHTING in a combat shot, as ids - the rest of the cast watches.

    The fight-arc 分镜 puts the onlookers in the fight's cast (three layers:
    duel, elders behind, 段誉 in front), and C01S02 (2026-09-30) came back with
    FOUR people fighting: everyone in the cast had been handed a drawn sword
    and nobody had been told who the two were. Whoever the action and beats
    NAME is a fighter; failing that, the first two people listed (the 分镜
    lists the duel first)."""
    if not is_combat(shot):
        return []
    by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or []) if c.get("id")}
    ppl = [i for i in cast_ids(shot, bp) if not is_group(by_id.get(i))]
    txt = str(shot.get("action") or "") + "".join(
        str(b.get("do") or "") for b in (shot.get("action_beats") or []) if isinstance(b, dict))
    named = []
    for i in ppl:
        c = by_id[i]
        nm = str(c.get("name") or "")
        names = [re.sub(r"[（(].*?[）)]", "", nm).strip()] + re.findall(r"[（(](.*?)[）)]", nm)
        if any(n and len(n) >= 2 and n in txt for n in names):
            named.append(i)
    if len(named) >= 2:
        return named
    out = named + [i for i in ppl if i not in named]
    return out[:2]


def _fighters_from_png(png, temperature=0):
    """How many people are FIGHTING - holding a bare blade in a fighting
    stance or mid-strike. None = no verdict."""
    try:
        ans = _vlm_json(
            "画面里有几个人手里拿着出鞘的剑或刀、正在打斗或摆着打斗的架势？"
            "只数正在交手的人；坐着、站着旁观的人不算，剑在鞘里的不算。"
            '只输出 {"打斗人数": 数字}。', png, prefill='{"打斗人数":', temperature=temperature)
    except Exception:                                       # noqa: BLE001
        return None
    m = re.search(r"\d+", str((ans or {}).get("打斗人数", "")))
    return int(m.group(0)) if m else None


def fighter_check(path, want, at=None):
    """(ok, n) - a still (at=None) or one frame of a clip. Two looks before a
    failure, like the head count."""
    try:
        if at is None:
            with open(path, "rb") as f:
                png = f.read()
        else:
            png = _grab(path, at)
    except Exception:                                       # noqa: BLE001
        return None, None
    n = _fighters_from_png(png)
    if n is None or n <= want:
        return (None if n is None else True), n
    again = _fighters_from_png(png, temperature=0.4)
    if again is None or again <= want:
        return True, n
    return False, again


def clip_fighters(path, want):
    """(ok, [(t, n)]) over the same frames the head count samples."""
    dur = _duration(path) or 0.0
    seen, bad = [], None
    for f in CLIP_CHECK_AT:
        ok, n = fighter_check(path, want, at=dur * f)
        if n is not None:
            seen.append((round(dur * f, 2), n))
        if ok is False:
            bad = True
    if not seen:
        return None, []
    return (False if bad else True), seen


def _heads_from_png(png, temperature=0):
    """How many people the vision model sees in one picture. None = no verdict."""
    try:
        # Ask it to be SURE rather than to be thorough. "包括远处很小的人"
        # read a blue barrel by a woodpile as a man and would have refused to
        # render C01S02 at all, while the strict wording below separates the
        # real case from the invented one cleanly: the plate with a man at the
        # end of the lane answers 1/远处小径尽头 twice, the empty plate beside
        # it answers 0/无 twice. A false positive here costs a shot that never
        # renders; a false negative costs one re-render.
        ans = _vlm_json(
            "画面里有没有人？只数能明确看出是人的（看得到头和身体、四肢）。"
            "远处看不清、拿不准是不是人的小点不算；器物、木桶、柴堆、晾着的衣服、"
            "画里画的人、影子、雕像都不算。"
            '只输出 {"人数": 数字, "位置": "每个人在哪，很简短；没有人就写 无"}。',
            png, prefill='{"人数":', temperature=temperature)
    except Exception:                                       # noqa: BLE001
        return None
    v = str((ans or {}).get("人数", "")).strip()
    m = re.search(r"\d+", v)
    if m:
        return int(m.group(0))
    for word, k in (("没有", 0), ("零", 0), ("一", 1), ("两", 2), ("二", 2), ("三", 3), ("四", 4)):
        if word in v:
            return k
    return None


def _heads_in_frame(path, at, temperature=0):
    """How many people are in the frame of `path` at `at` seconds."""
    try:
        return _heads_from_png(_grab(path, at), temperature=temperature)
    except Exception:                                       # noqa: BLE001
        return None


def image_headcount(path, want):
    """(ok, n) for a still. Same two-look discipline as the clip check."""
    try:
        with open(path, "rb") as f:
            png = f.read()
    except OSError:
        return None, None
    n = _heads_from_png(png)
    if n is None or n <= want:
        return (None, n) if n is None else (True, n)
    again = _heads_from_png(png, temperature=0.4)
    if again is None or again <= want:
        return True, n
    return False, again


def clip_headcount(path, want):
    """(ok, [(t, n)]) for a finished clip. ok=None when there is no verdict.

    Sampled from 30% in: the opening frames ARE the pinned first frame, which
    on a cold shot is a plate with nobody in it, and counting those would
    report nought people for every cold clip in the book.
    """
    dur = _duration(path) or 0.0
    if dur <= 0:
        return None, []
    seen = []
    for f in CLIP_CHECK_AT:
        n = _heads_in_frame(path, dur * f)
        if n is not None:
            seen.append((round(dur * f, 2), n))
    if not seen:
        return None, []
    over = [(t, n) for t, n in seen if n > want]
    if not over:
        return True, seen
    # A second look at the worst frame only. The model has called one sheathed
    # sword "two, one drawn" before, and two GPU-hours is too much to spend on
    # one opinion.
    t, n = max(over, key=lambda r: r[1])
    again = _heads_in_frame(path, t, temperature=0.4)
    if again is None or again <= want:
        return True, seen + [(t, again if again is not None else -1)]
    return False, seen + [(t, again)]


def _crop_caption(src, dst, keep=None):
    """Drop the bottom of the frame and re-frame to the same canvas.

    Top-aligned: the caption is at the bottom, so the bottom is what goes.
    The sides lose the same fraction to keep the aspect ratio, which is what
    the 4K pass already does when it covers-and-crops.
    """
    keep = float(keep or CAPTION_KEEP)
    par = _stream_params(src)
    if not par:
        raise RuntimeError("读不到画面参数")
    w, h, fps, tbn = par
    ch = (int(h * keep) // 2) * 2
    cw = (int(ch * (w / float(h))) // 2) * 2
    args = ["-i", src, "-vf",
            "crop=%d:%d:%d:0,scale=%d:%d:flags=lanczos" % (cw, ch, (w - cw) // 2, w, h),
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "16",
            "-c:a", "copy", "-movflags", "+faststart", "-r", "%g" % fps]
    if tbn:
        args += ["-video_track_timescale", str(tbn)]
    _ff(args + [dst], timeout=900)
    return dst


# ----------------------------------------------------------------------
# lip check - a mouth moving where nothing is being said
# ----------------------------------------------------------------------
# The narration is voice-over and h3 animates a talking mouth anyway; that is
# what `_mouth_clause` is for. What it could not see until 2026-09-18 is a shot
# that DOES speak and then stops: C01S08 is 11.25 s of picture over 7.24 s of
# whispered dialog, and its last four seconds were a mouth moving against
# silence. The clause is per segment now, so new clips cannot carry the defect
# - but everything already rendered can, and only a look at the picture says
# which.
#
# Half the job is free: the speech timeline is NOT measured, it is recorded.
# Every take sidecar stores each line's start and end inside the clip, so the
# silent windows are known exactly and the only question left is what the mouth
# is doing inside them.
#
# The pixel route was tried first and thrown out. Watching mouth-region motion
# needs to know where the mouth IS, and this box has no face detector - no cv2,
# no mediapipe, no dlib. Projecting the previz skeleton's nose through the
# shot's own camera works when the render followed the control and fails
# silently when it did not: measured on three clips, the box landed on the
# mouth for C01S08, on a shoulder for C01S03 and ON A ROOFTOP for C01S07 -
# which is the shot that route then flagged. A detector that is confidently
# wrong about which pixels are a face is worse than no detector.
#
# So it asks the model that already verifies every reference image, with the
# same machinery: 1280-px JPEG, prefilled assistant turn, an empty reply is "no
# verdict" rather than "failed", and a problem is only believed when a second
# call at a higher temperature sees it too.
# Run automatically as each clip lands, like the image verifier. Off by env if
# the GPU is needed for nothing else.
LIPCHECK_AUTO = os.environ.get("BOOKREEL_LIPCHECK", "1") not in ("0", "", "false")
LIPCHECK_MIN_GAP = float(os.environ.get("BOOKREEL_LIPCHECK_MIN_GAP", "1.0"))
# A shot that starts COLD opens on the location plate - 空景无人 by design - and
# h3 takes about a second to bring the subject into frame. C01S14, measured
# 2026-09-18: empty study at 0.15 s and 0.45 s, a face sliding in from the
# right edge at 0.75 s, fully framed at 1.0 s; the line 别碰 played 0.24-0.70 s.
# The voice was spoken before the speaker existed. So a cold shot with dialog
# gets its sound delayed until the speaker can be there. Narration is
# voice-over and needs no one in frame, so it is left alone.
COLD_LEAD_SECONDS = float(os.environ.get("BOOKREEL_COLD_LEAD", "1.0"))
LIPCHECK_EDGE = 0.35          # keep off the boundary: a mouth closing is not a fault
LIPCHECK_FRAMES = int(os.environ.get("BOOKREEL_LIPCHECK_FRAMES", "3"))
_LIP_OPEN = ("张", "说", "讲", "开", "动")
_LIP_SHUT = ("闭", "合", "没有", "未", "静")


def _silent_spans(meta):
    """[(start, end)] inside the clip where no line is playing.

    From the sidecar's own cues, trimmed at both edges: a frame taken the
    instant a line ends catches a mouth still closing, and that is not a fault.
    """
    secs = float(meta.get("seconds") or 0)
    cues = sorted(((float(c["start"]), float(c["end"])) for c in (meta.get("cues") or [])),
                  key=lambda c: c[0])
    spans, t = [], 0.0
    for a, b in cues + [(secs, secs)]:
        if a - t >= LIPCHECK_MIN_GAP:
            spans.append((t + LIPCHECK_EDGE, a - LIPCHECK_EDGE))
        t = max(t, b)
    return [(a, b) for a, b in spans if b - a > 0.2]


def _grab(path, t):
    """One frame as PNG bytes."""
    import tempfile
    fd, out = tempfile.mkstemp(suffix=".png", prefix="lip-")
    os.close(fd)
    _ff(["-ss", "%.2f" % max(0.0, t), "-i", path, "-vframes", "1", out], timeout=120)
    try:
        with open(out, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _ask_mouth(png):
    """{'嘴': ..., '人': ...} for one frame, or {} when the model says nothing."""
    prompt = ("看这一帧，只回答画面里最靠前的那个人，输出一个 JSON 对象，值要短：\n"
              '- "人": 画面里有没有看得清的人脸？有/没有\n'
              '- "嘴": 这个人的嘴是闭着的，还是张开像在说话？闭合/张开\n')
    return _vlm_json(prompt, png, prefill='{"人":')


def _mouth_open(ans):
    """True / False / None - is the mouth open in this frame?"""
    if not isinstance(ans, dict):
        return None
    if any(w in str(ans.get("人", "")) for w in ("没有", "无", "不")):
        return None                                   # no face in frame: no opinion
    v = str(ans.get("嘴", "")).strip()
    if not v:
        return None
    if any(w in v for w in _LIP_SHUT):
        return False
    return True if any(w in v for w in _LIP_OPEN) else None


def lip_path(slug, n, sid):
    return os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), "_takes", sid, "lip.json")


def lipcheck_shot(slug, n, shot):
    """Look at this clip's silent windows and say whether the mouth moves in them."""
    sid = str(shot.get("id") or "")
    clip = os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), sid + ".mp4")
    if not os.path.isfile(clip) or not (shot.get("characters") or []):
        return None
    takes = os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), "_takes", sid)
    take = (_read_json(os.path.join(takes, "_active.json"), {}) or {}).get("take")
    meta = _read_json(os.path.join(takes, "%s.json" % take), {}) or {}
    if not meta.get("cues"):
        return {"ok": True, "note": "这一镜没有台词或旁白的时间轴，跳过", "hits": [],
                "take": take, "checked": 0, "model": VERIFY_MODEL}
    spans = _silent_spans(meta)
    if not spans:
        return {"ok": True, "note": "全程都在说话，没有静音窗口", "hits": [],
                "take": take, "checked": 0, "model": VERIFY_MODEL}
    # Spread the samples over the silent time rather than taking one frame per
    # window: C01S08 has exactly one gap, and a mouth that is shut at 9.2 s can
    # be moving at 10.1. Longest windows first, then a point roughly every
    # 1.2 s inside them, capped.
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    times = []
    for a, b in spans:
        # C01S14's only silent window was 2.35 s and got ONE frame at 2.2 s,
        # which happened to be closed; the mouth was open at 3.0 s. A point
        # every 0.8 s, up to the cap.
        k = min(LIPCHECK_FRAMES, max(1, int(round((b - a) / 0.8))))
        times += [a + (b - a) * (j + 0.5) / k for j in range(k)]
        if len(times) >= LIPCHECK_FRAMES:
            break
    times = times[:LIPCHECK_FRAMES]
    t0, hits, checked, seen = time.time(), [], 0, 0
    for t in times:
        try:
            png = _grab(clip, t)
            ans = _ask_mouth(png)
        except Exception as exc:                            # noqa: BLE001
            return {"ok": None, "note": "审核模型不可用：%s" % str(exc)[:110], "hits": hits,
                    "take": take, "checked": checked, "model": VERIFY_MODEL,
                    "seconds": int(time.time() - t0)}
        v = _mouth_open(ans)
        if v is None:
            continue                                        # no face, or no answer
        seen += 1
        checked += 1
        if not v:
            continue
        # Asked again, warmer, before it counts - the same rule the image
        # verifier follows, for the same reason.
        try:
            if _mouth_open(_vlm_json(
                    "看这一帧，这个人的嘴是闭着的还是张开像在说话？"
                    '只输出 {"嘴": "闭合"} 或 {"嘴": "张开"}。', png,
                    temperature=0.4, prefill='{"嘴":')) is not True:
                continue
        except Exception:                                   # noqa: BLE001
            continue
        hits.append(round(t, 2))
    if not seen:
        return {"ok": None, "note": "这些静音帧里看不到人脸，没有结论", "hits": [],
                "take": take, "checked": checked, "model": VERIFY_MODEL,
                "seconds": int(time.time() - t0)}
    return {"ok": not hits, "hits": hits, "take": take, "checked": checked,
            "note": ("静音处仍在动嘴：%s 秒" % "、".join("%.1f" % h for h in hits)) if hits
                    else "静音处嘴是闭的",
            "model": VERIFY_MODEL, "seconds": int(time.time() - t0)}


def lipcheck_chapter(slug, n, keys=None, force=False):
    """Every clip of a chapter, skipping ones already checked since they landed."""
    sc = _read_json(os.path.join(PROJECTS, slug, "script", "%02d.json" % int(n)), {}) or {}
    shots = [s for s in (sc.get("shots") or []) if s.get("id")]
    if keys:
        want = set(str(k).split(":")[-1] for k in keys)
        shots = [s for s in shots if s["id"] in want]
    done, skipped, flagged = [], [], []
    t0 = time.time()
    for sh in shots:
        sid = sh["id"]
        clip = os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), sid + ".mp4")
        out = lip_path(slug, n, sid)
        if not os.path.isfile(clip):
            continue
        if not force and os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(clip):
            skipped.append(sid)
            continue
        r = lipcheck_shot(slug, n, sh)
        if r is None:
            continue
        r["id"] = sid
        r["at"] = int(time.time())
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(r, fh, ensure_ascii=False)
        except OSError:
            pass
        done.append(sid)
        if r.get("ok") is False:
            flagged.append({"id": sid, "hits": r.get("hits") or []})
    return {"ok": True, "slug": slug, "n": int(n), "checked": done, "skipped": skipped,
            "flagged": flagged, "seconds": round(time.time() - t0, 1)}


def lip_items(slug, n):
    """One row per clip: what the check said, for the panel."""
    sc = _read_json(os.path.join(PROJECTS, slug, "script", "%02d.json" % int(n)), {}) or {}
    rows = []
    for sh in (sc.get("shots") or []):
        sid = sh.get("id")
        if not sid or not (sh.get("characters") or []):
            continue
        r = _read_json(lip_path(slug, n, sid), None)
        if not isinstance(r, dict):
            continue
        rows.append({"id": sid, "ok": r.get("ok"), "hits": r.get("hits") or [],
                     "note": r.get("note") or "", "checked": r.get("checked") or 0})
    return rows


# ----------------------------------------------------------------------
# previz - the 3D staging pass
# ----------------------------------------------------------------------
# A shot reaches h3 as text plus one still frame, and everything spatial in the
# script is prose the renderer drops. That is why h3 drew its single subject
# twice (C01S02, C01S08) and why 74% of shots start cold on a static plate: an
# angle change means new geometry, and a crop cannot fake geometry.
#
# So the shot is staged in 3D first - rooms and bodies as numbers
# (services/stage.py), rasterised to a control video (services/previz.py) - and
# the video model is asked to follow it. A body that exists once in a room
# cannot be drawn twice.
#
# Rendered INLINE rather than as a sixth queue, for the same reason the chapter
# film is: 90 frames of three views takes about 6 seconds of CPU against ~27
# GPU-minutes for the segment it feeds, so it never competes for the GPU and
# never needs a place in a line.
PREVIZ_ENABLED = os.environ.get("BOOKREEL_PREVIZ", "1") not in ("0", "", "false")
# Which of the three renders is handed to the ControlNet. A skeleton is the
# strongest signal for a human subject; an empty scene has no skeleton to draw,
# so it falls back to depth.
PREVIZ_CONTROL = os.environ.get("BOOKREEL_PREVIZ_CONTROL", "pose")
# How hard the picture is held to the previz. Too high and the boxes show
# through as a mannequin; too low and it is decoration. The proxy is a crude
# figure, not a performance, so it starts well under 1.0.
PREVIZ_STRENGTH = float(os.environ.get("BOOKREEL_PREVIZ_STRENGTH", "0.6"))
# How far a body has to travel, IN PIXELS of the rendered canvas, before its
# previz is worth handing over. Measured 2026-09-17 across chapter 1: a staging
# the model wrote as "different" by 4 degrees of yaw travels 5 px and is
# invisible; a body that stands up or walks travels 150-1700. 12 px is about 1%
# of the 1312-px canvas. Below it the control video says "hold still", which is
# the one thing it must never say by accident.
PREVIZ_MOTION_PX = float(os.environ.get("BOOKREEL_PREVIZ_MOTION_PX", "12"))
# The depth branch is dense - the room fills the frame - and a locked camera in
# a still room renders bit-identical frames, so anything above noise is real.
PREVIZ_MOTION_DEPTH = float(os.environ.get("BOOKREEL_PREVIZ_MOTION_DEPTH", "0.02"))


CONTROL_CKPT = "MiniMax-H3-Fun-Controlnet-Union.safetensors"


def _control_model_present():
    """The ControlNet branch h3 needs to act on a previz at all."""
    return os.path.isfile(os.path.join(ROOT, "ComfyUI", "models",
                                       "model_patches", CONTROL_CKPT))


def previz_argv(py, job_path="-"):
    """The previz command, marked as the kernel's FIRST choice to kill.

    A chapter-wide rebuild is ~0.8 GB of numpy per shot and it runs happily
    beside a render - 6 s of CPU against 27 GPU-min - but ComfyUI holds 21-25 GB
    of this 30 GB box, so "happily" was down to 3 GB available, measured
    2026-09-17. The oom-killer picks the largest RSS, which is always the
    render. `oom_score_adj=1000` makes it pick this instead: the rule this file
    already states for anything heavy run beside a render, applied to the one
    thing that now runs beside every render. Written through a shell rather
    than `preexec_fn`, which is not safe in a threaded server.
    """
    return ["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
            py, os.path.join(ROOT, "services", "previz.py"), job_path]


def previz_py():
    """The renderer runs in venvs/tools - the one with numpy and PIL.
    web_server itself is stdlib-only and must stay that way."""
    for c in (os.path.join(ROOT, "venvs", "tools", "bin", "python"),
              os.path.join(ROOT, "venvs", "tools", "Scripts", "python.exe")):
        if os.path.isfile(c):
            return c
    return None


def previz_dir(slug, n):
    return os.path.join(PROJECTS, slug, "previz", "%02d" % int(n))


def previz_stem(slug, n, shot_id, i):
    return os.path.join(previz_dir(slug, n), "%s-seg%d" % (shot_id, i))


def previz_motion(slug, n, shot_id, i):
    """What previz.py measured for this segment, or None if it predates it."""
    m = _read_json("%s-motion.json" % previz_stem(slug, n, shot_id, i), None)
    return m if isinstance(m, dict) else None


def previz_moves(slug, n, shot_id, i, kind, shot=None):
    """Does this segment's control video actually move?

    The number previz.py wrote beside the video wins, because it was taken on
    the pixels that were drawn: body travel through a HELD camera for pose (a
    lock-off push slides every joint across the frame without the body doing
    anything, and that is the case that was measured as harmful), the frame's
    own change for depth. `stage.control_useful` is only the fallback for a
    previz built before the measurement existed - it reads the staging, so it
    cannot tell a 15-degree turn in a close-up from the same turn in an
    extreme long shot.
    """
    m = previz_motion(slug, n, shot_id, i)
    if m is None:
        return shot is None or stage.control_useful(shot, kind, _shot_seconds(shot))
    if kind == "depth":
        return float(m.get("depth_d") or 0.0) > PREVIZ_MOTION_DEPTH
    return float(m.get("pose_px") or 0.0) >= PREVIZ_MOTION_PX


def previz_control(slug, n, shot_id, i, kind=None, shot=None):
    """The control video for one segment, or "" when there is none.

    A skeleton is the strongest signal for a human subject, so a shot with
    people in it is controlled on pose. An insert with nobody in it has no
    skeleton to draw, so it falls back to the room's depth - which still pins
    the geometry the next shot has to match.
    """
    if not kind:
        kind = PREVIZ_CONTROL
        if shot is not None and not (shot.get("characters") or []):
            kind = "depth"
    # A previz that does not MOVE is a freeze order, and h3 obeys it: 27 of 27
    # staged shots of xuemang chapter 1 came out with a still pose video and the
    # clips rendered against them lost up to two thirds of their motion. So the
    # previz has to earn the hand-off (stage.control_useful); when it does not,
    # this returns "" and the render falls through to exactly what it did before
    # previz existed. The 3D pass still runs - the panel's -previz.mp4 is how a
    # person checks blocking - it is only the CONTROL that is withheld.
    if not previz_moves(slug, n, shot_id, i, kind, shot):
        return ""
    p = "%s-%s.mp4" % (previz_stem(slug, n, shot_id, i), kind)
    if os.path.isfile(p):
        return p
    # Asked for a pose that was never drawn (no bodies): depth is still there.
    alt = "%s-depth.mp4" % previz_stem(slug, n, shot_id, i)
    if kind == "depth" or not os.path.isfile(alt):
        return ""
    return alt if previz_moves(slug, n, shot_id, i, "depth", shot) else ""


# CosyVoice's measured pace on this box: 0.324 s a character over the 23 voiced
# shots of chapter 1 (mean error 9%, p90 19%). Good enough to PLAN a previz,
# never good enough to be believed - see _shot_seconds.
# The arithmetic itself lives in stage.py: /stage/shots has to ask for the same
# number of beats this estimate implies segments, and story_server cannot import
# this file. Same env vars, same defaults, one implementation.
SPEECH_SECONDS_PER_CHAR = stage.SPEECH_SECONDS_PER_CHAR


def _spoken_chars(shot):
    """Characters that will actually be read aloud in this shot."""
    return stage.spoken_chars(shot)


def _shot_seconds(shot, seg=None):
    """How long this shot will really run - the same arithmetic _render does.

    NOT `shot["seconds"]`. That is the script's wish; the clip's length is set
    by the NARRATION, because the mux is -shortest and cutting the picture
    would cut the line. On chapter 1 the two disagreed on 23 of 30 shots -
    C01S12 is written as 6 s and renders as 15 - so a previz planned off
    `seconds` handed the renderer two control videos for a four-segment shot
    and left half of it uncontrolled.
    """
    return stage.shot_seconds(shot, handle=VIDEO_HANDLE_SECONDS,
                              lo=VIDEO_MIN_SECONDS, hi=VIDEO_VOICE_MAX_SECONDS,
                              silent=VIDEO_SILENT_SECONDS,
                              silent_wide=VIDEO_SILENT_WIDE_SECONDS)


def _segments_for(secs, seg=None):
    return stage.segments_for(secs, seg or SEG_SECONDS)


def _shot_segments(shot, seg=None):
    """How many segments this shot will be rendered in."""
    return _segments_for(_shot_seconds(shot, seg), seg)


def previz_shot_job(slug, n, shot, bp, rooms, fmt="16:9", seg=None, nseg=None,
                    seconds=None, crop=None):
    """The job dict for one shot, or None when it cannot be staged.

    None means the shot has no `staging` - an un-migrated chapter - and the
    caller falls straight through to what the renderer did before previz
    existed. Nothing regresses for a chapter that has not been staged.
    """
    # The room is per location; indoors-or-out is per SHOT, because a location
    # can be two spaces (沈家小院 is a courtyard and the study in it). The slug
    # line is the only field that says which one this shot is, and /stage keeps
    # the two plans apart under lo01#内 and lo01#外.
    inside = stage.shot_interior(shot)
    room = stage.room_for(rooms, shot.get("location_id"), inside)
    if inside is not None and bool(room.get("interior")) != inside:
        room = dict(room)
        room["interior"] = inside
    by_id = {str(c.get("id")): c for c in (bp.get("characters") or []) if c.get("id")}
    heights = {cid: stage.character_height(c) for cid, c in by_id.items()}
    secs = float(seconds or 0) or _shot_seconds(shot, seg)
    nseg = int(nseg or _segments_for(secs, seg))
    nbeats = len(shot.get("action_beats") or []) or nseg
    marks = stage.shot_marks(shot, room, heights, nbeats)
    if not marks or not any(m for m in marks):
        return None
    cast = [c for c in (shot.get("characters") or []) if any(str(c) in m for m in marks)]
    # An empty insert still previzes - the room's geometry is a perfectly good
    # control signal, and depth is what it is handed. Only the pose video has
    # nothing to draw, which previz_control already knows.

    res = VIDEO_FORMATS.get(fmt or "16:9", VIDEO_RESOLUTION)
    # The previz must be drawn at the canvas the CLIP is rendered at, not at
    # the delivery format: the ControlNet keeps the control video's own aspect,
    # and 1920x1080 previz under a 1312x736 render would letterbox the control.
    w, h = _canvas_for(res)
    aspect = float(w) / float(h)
    side = stage.axis_side(marks, cast[0], cast[1] if len(cast) > 1 else None)
    frames = int(round((seg or SEG_SECONDS) * 24))
    segs = []
    for i in range(nseg):
        t0 = i / float(nseg)
        t1 = (i + 1) / float(nseg)
        sub = [dict(m) for m in marks]
        # solve_camera walks t across the WHOLE shot, so a segment is a WINDOW
        # of that solve rather than its own: a 推 running the length of a
        # 15-second shot must move a quarter of its distance in each of four
        # segments, not the whole distance four times over.
        cams = _window_cams(shot, room, sub, cast, aspect, frames, side, i, nseg,
                            seconds=secs)
        # THE CONTROL VIDEO MUST BE FRAMED LIKE THE CLIP, NOT LIKE THE SCRIPT.
        # A shot that inherits the previous shot's last frame is handed it
        # centre-cropped (`_inherit_ratio`, `_crop_frame`), so the picture
        # starts at a tighter framing than the solver computed from shot_size.
        # Measured on C01S02, 2026-09-19: inherited at 70%, the clip is a tight
        # two-hands shot while the pose video showed one small centred body -
        # and h3, given a big body in the frame and a small one in the control,
        # drew TWO men writing side by side, which is the very failure previz
        # exists to prevent. Cropping the centre by r narrows the field of view
        # to 2*atan(r*tan(fov/2)); apply it to every solved frame.
        if crop and 0.05 < float(crop) < 1.0:
            r = float(crop)
            for c in cams:
                c["vfov"] = math.degrees(2.0 * math.atan(
                    r * math.tan(math.radians(c["vfov"]) / 2.0)))
        segs.append({"stem": previz_stem(slug, n, shot["id"], i),
                     "t0": t0, "t1": t1, "f0": i * frames, "cams": cams})
    c = shot.get("camera") or {}
    slate = " ".join(x for x in (
        shot["id"],
        "·", str(c.get("shot_size") or ""), str(_plain(c.get("angle")) or ""),
        ("%smm" % c.get("lens_mm")) if c.get("lens_mm") else "",
        str(_plain(c.get("movement")) or ""),
        "·", str(shot.get("location_id") or ""),
        ("室外" if inside is False else ("室内" if inside else "")),
        "%.1fx%.1fm" % (room["size"][0], room["size"][1])) if x)
    return {"width": w, "height": h, "room": room, "marks": marks, "cast": cast,
            "slate": slate,
            # Where the person's one-file view of the whole shot goes.
            "stem": os.path.join(previz_dir(slug, n), str(shot["id"])),
            # Colour is keyed on the PROJECT's cast order, not this shot's.
            # Keyed on the shot's, every single-character shot came out red -
            # index 0 in a one-element list - so a contact sheet said nothing
            # about who was who, which is the one job the colour has.
            "palette": [str(c.get("id")) for c in (bp.get("characters") or []) if c.get("id")],
            "names": {cid: (by_id.get(cid) or {}).get("name") or "" for cid in cast},
            "ffmpeg": _ffmpeg(), "segments": segs}


def _window_cams(shot, room, marks, cast, aspect, frames, side, i, nseg,
                 seconds=None):
    """Camera for segment i, solved on the shot's own clock.

    A 推 that runs the length of a 15-second shot must move a fifteenth of its
    distance in each of four segments, not the whole distance four times.
    """
    whole = stage.solve_camera(shot, room, marks, cast, aspect,
                               frames * nseg, side=side, seed=0, seconds=seconds)
    return whole[i * frames:(i + 1) * frames] or whole[-frames:]


def _canvas_for(res):
    """The canvas h3 will actually render, mirroring its own _legal_canvas:
    multiples of 32 inside the pixel budget at the asked ratio."""
    try:
        rw, rh = (int(v) for v in str(res).lower().split("x")[:2])
    except (ValueError, TypeError):
        rw, rh = 1920, 1080
    want = float(rw) / float(rh or 1)
    budget = int(os.environ.get("H3_MAX_PIXELS", str(1032192)))
    best = None
    for w in range(32, 1920 + 32, 32):
        for h in range(32, 1920 + 32, 32):
            if w * h > budget:
                continue
            if abs((float(w) / h) - want) / want > 0.01:
                continue
            if best is None or w * h > best[0] * best[1]:
                best = (w, h)
    return best or (1312, 736)


def _ensure_previz(slug, n, shot, bp, nseg, seconds, fmt="16:9", crop=None):
    """Previz for one shot at an exact segment count, rebuilt if it is stale.

    Cheap enough to do inside the render loop - about 6 seconds of CPU against
    ~22 GPU-minutes for one of the segments it feeds.
    """
    # Existing is not the same as current: re-running 走位 rewrites the script's
    # staging, and a previz built from the OLD staging would otherwise be handed
    # to the model forever. Same idempotency rule as the 4K pass - a file at
    # least as new as its source is that source's; older, and it is redone.
    # Both inputs count: the script carries the staging, stage.json carries the
    # room it is staged in, and re-measuring a location (splitting 沈家小院 into
    # lo01#内 and lo01#外) changes every frame of the previz without touching a
    # single shot.
    src_m = 0.0
    for src in (os.path.join(PROJECTS, slug, "script", "%02d.json" % int(n)),
                os.path.join(PROJECTS, slug, "assets", "stage.json")):
        try:
            src_m = max(src_m, os.path.getmtime(src))
        except OSError:
            pass

    def _fresh(i):
        f = "%s-motion.json" % previz_stem(slug, n, shot["id"], i)
        try:
            return os.path.getmtime(f) >= src_m
        except OSError:
            return False

    # A shot whose inherited crop changed needs a new previz even though
    # nothing on disk did: the framing is part of the job, not of the sources.
    stamp = os.path.join(previz_dir(slug, n), "%s.crop" % shot["id"])
    want = "%.3f" % float(crop or 1.0)
    try:
        with open(stamp, encoding="utf-8") as fh:
            had = fh.read().strip()
    except OSError:
        had = ""
    if had != want:
        try:
            os.makedirs(previz_dir(slug, n), exist_ok=True)
            with open(stamp, "w", encoding="utf-8") as fh:
                fh.write(want)
        except OSError:
            pass
        have = False
    else:
        have = all(_fresh(i) for i in range(nseg))
    extra = os.path.isfile("%s-motion.json" % previz_stem(slug, n, shot["id"], nseg))
    if have and not extra:
        return False
    py = previz_py()
    rooms = _read_json(os.path.join(PROJECTS, slug, "assets", "stage.json"), {}) or {}
    if not py or not rooms:
        return False
    job = previz_shot_job(slug, n, shot, bp, rooms, fmt, nseg=nseg, seconds=seconds,
                          crop=crop)
    if not job:
        return False
    os.makedirs(previz_dir(slug, n), exist_ok=True)
    pr = subprocess.run(previz_argv(py),
                        input=json.dumps(job, ensure_ascii=False).encode("utf-8"),
                        capture_output=True, timeout=900)
    if pr.returncode != 0:
        raise RuntimeError(pr.stderr.decode("utf-8", "replace").strip()[-240:])
    # A shot that got shorter leaves control videos for segments that no longer
    # exist; they would never be read, but they make the panel lie about what
    # is built.
    i = nseg
    while os.path.isfile("%s-motion.json" % previz_stem(slug, n, shot["id"], i)):
        for suffix in ("previz.mp4", "depth.mp4", "pose.mp4", "motion.json"):
            try:
                os.unlink("%s-%s" % (previz_stem(slug, n, shot["id"], i), suffix))
            except OSError:
                pass
        i += 1
    return True


def build_previz(slug, n, keys=None, fmt="16:9"):
    """Render the previz for one chapter. Returns a small report.

    Errors are swallowed per shot into the report rather than raised: a shot
    that will not stage is a thing to read, not a reason to stop the other
    twenty-nine - the same rule the chapter film's join follows.
    """
    py = previz_py()
    if not py:
        return {"ok": False, "error": "venvs/tools 没有 python（numpy + PIL 在那里）"}
    base = os.path.join(PROJECTS, slug)
    sc = _read_json(os.path.join(base, "script", "%02d.json" % int(n)), {}) or {}
    bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
    rooms = _read_json(os.path.join(base, "assets", "stage.json"), {}) or {}
    shots = [s for s in (sc.get("shots") or []) if s.get("id")]
    if keys:
        want = set(str(k).split(":")[-1] for k in keys)
        shots = [s for s in shots if s["id"] in want]
    if not shots:
        return {"ok": False, "error": "第 %s 章没有镜头" % n}
    if not rooms:
        return {"ok": False, "error": "还没有场景平面图：先在故事服务上 POST /stage"}

    os.makedirs(previz_dir(slug, n), exist_ok=True)
    done, skipped, failed = [], [], []
    t0 = time.time()
    for sh in shots:
        job = previz_shot_job(slug, n, sh, bp, rooms, fmt)
        if not job:
            skipped.append(sh["id"])
            continue
        try:
            pr = subprocess.run(previz_argv(py),
                                input=json.dumps(job, ensure_ascii=False).encode("utf-8"),
                                capture_output=True, timeout=900)
            if pr.returncode != 0:
                raise RuntimeError(pr.stderr.decode("utf-8", "replace").strip()[-300:])
            done.append(sh["id"])
        except Exception as exc:                            # noqa: BLE001
            failed.append({"id": sh["id"], "error": str(exc)[:200]})
    return {"ok": True, "slug": slug, "n": int(n), "built": done,
            "skipped": skipped, "failed": failed,
            "seconds": round(time.time() - t0, 1)}


def previz_items(slug, n):
    """One row per shot: whether it is staged, and whether previz exists."""
    base = os.path.join(PROJECTS, slug)
    sc = _read_json(os.path.join(base, "script", "%02d.json" % int(n)), {}) or {}
    rows = []
    for sh in (sc.get("shots") or []):
        sid = sh.get("id")
        if not sid:
            continue
        nseg = _shot_segments(sh)
        st = sh.get("staging") or {}
        beats = (st.get("beats") if isinstance(st, dict) else None) or []
        made = [i for i in range(nseg)
                if os.path.isfile("%s-motion.json" % previz_stem(slug, n, sid, i))]
        # One file for the whole shot. The panel used to play seg0, so a
        # fifteen-second shot previewed as 3.75 seconds of itself.
        f = os.path.join(previz_dir(slug, n), "%s-previz.mp4" % sid)
        whole = os.path.isfile(f)
        if not whole and made:
            # Built before the join existed: still playable, still only the
            # first 3.75 s. A row that plays a segment beats an empty <video>,
            # which is what the page showed for every shot but the one that had
            # been rebuilt - "只有第一个能点".
            f = "%s-previz.mp4" % previz_stem(slug, n, sid, made[0])
        url = ""
        if os.path.isfile(f):
            url = "/media/%s/previz/%02d/%s?t=%d" % (
                slug, int(n), os.path.basename(f), int(os.path.getmtime(f)))
        # Built is not the same as used: a previz whose bodies and camera both
        # stand still is rendered for the person and withheld from the model.
        kind = "depth" if not (sh.get("characters") or []) else PREVIZ_CONTROL
        ctl = any(previz_moves(slug, n, sid, i, kind, sh) for i in made) if made \
            else stage.control_useful(sh, kind, _shot_seconds(sh))
        px = max([float((previz_motion(slug, n, sid, i) or {}).get("pose_px") or 0.0)
                  for i in made] or [0.0])
        rows.append({"id": sid, "location": sh.get("location_id") or "",
                     "shot_size": (sh.get("camera") or {}).get("shot_size") or "",
                     "movement": (sh.get("camera") or {}).get("movement") or "",
                     "characters": [c for c in (sh.get("characters") or [])],
                     "staged": bool(beats), "beats": len(beats),
                     "segments": nseg, "built": len(made), "url": url,
                     "controls": bool(ctl), "motion_px": round(px, 1),
                     "whole": bool(whole)})
    return rows


# ----------------------------------------------------------------------
# the sound library (step 2)
# ----------------------------------------------------------------------
# ACE-Step CANNOT DO THIS and was tried anyway (2026-09-17). It is a song
# model: asked for a door, hooves, wind or cloth it returns music that gestures
# at them, and the whole first library generated with it was thrown out. The
# measurements that made it look workable - event-shaped output for a clap,
# 56-59% of the energy in the loudest tenth of the frames - measured the
# SHAPE of the audio and not what it was a recording of, which is the mistake.
# Envelope analysis cannot hear that a sound is wrong. Do not put foley back
# on the music model because the numbers look right.
#
# Stable Audio Open 1.0 is the sound-effects model, trained on Freesound, and
# ComfyUI supports it natively (ConditioningStableAudio + EmptyLatentAudio,
# CLIPLoader type "stable_audio"). It is also the only generator in this
# pipeline with a REAL negative branch - it runs an honest CFG, unlike Flux's
# BasicGuider and h3's ConditioningZeroOut - so here, uniquely, "music" can be
# subtracted rather than merely left unsaid.
#
# The weights are gated on Hugging Face (`stabilityai/stable-audio-open-1.0`,
# gated: auto): accept the licence once with an account and fetch with a
# token. Without them this queue does not run, and that is deliberate - no
# sound is better than the wrong sound, which is what shipped the first time.
SFX_WORKFLOW = os.path.join(ROOT, "comfy-stableaudio-sfx.json")
SFX_CKPT = "stable-audio-open-1.0.safetensors"
SFX_CLIP = "t5_base.safetensors"
# Length is asked per KIND, not once. Stable Audio is CONDITIONED on
# seconds_total, so asking a door for ten seconds asks for ten seconds of door
# - which the model fills with a montage of them, and the extractor then has
# to throw most of it away. A texture is the opposite case: it wants room to
# develop, and the clip is looped under the shot.
SFX_SECONDS = float(os.environ.get("BOOKREEL_SFX_SECONDS", "5"))
SFX_LOOP_SECONDS = float(os.environ.get("BOOKREEL_SFX_LOOP_SECONDS", "12"))
SFX_STEPS = int(os.environ.get("BOOKREEL_SFX_STEPS", "50"))
SFX_CFG = float(os.environ.get("BOOKREEL_SFX_CFG", "6.0"))
SFX_RETRIES = int(os.environ.get("BOOKREEL_SFX_RETRIES", "2"))
SFX_STALL = int(os.environ.get("BOOKREEL_SFX_STALL", str(30 * 60)))
# Subtracted, not merely unsaid - see above. A foley one-shot with a chord
# under it is unusable: it lands on the cut and announces itself as a sample.
# This is the BASE only: `_sfx_negative` adds the era's anachronisms
# (look.foley_negative) and the entry's own confusions (foley.SFX[...]["neg"])
# on top, and lets an entry take a word back out - 市集 is a crowd and would
# be emptied by the "speech, talking" here.
SFX_NEGATIVE = os.environ.get(
    "BOOKREEL_SFX_NEGATIVE",
    "music, melody, song, singing, vocals, choir, instrument, guitar, piano, "
    "drums, beat, rhythm, harmony, ambient pad, synth, soundtrack, "
    "speech, talking, applause")


def _sfx_negative(sp, bp):
    """Everything subtracted from one sound: the shared list, this world's
    anachronisms, and the entry's own confusions.

    Worth composing carefully here and nowhere else in the pipeline - Stable
    Audio is the only generator in it whose negative branch is a real CFG, so
    these words are the one case where naming what must not be there works
    (CLAUDE.md: Flux guides through BasicGuider, h3 zeroes its negative out).
    """
    return foley.negative_for(sp, SFX_NEGATIVE, look.foley_negative(bp))


def _sfx_model_present():
    """Both halves of the sound model, by the names the workflow asks for."""
    ck = os.path.join(ROOT, "ComfyUI", "models", "checkpoints", SFX_CKPT)
    cl = os.path.join(ROOT, "ComfyUI", "models", "text_encoders", SFX_CLIP)
    return os.path.isfile(ck) and os.path.isfile(cl)


def _ace_present():
    """ACE-Step on disk - the beds and the sound library share it."""
    return os.path.isfile(os.path.join(
        ROOT, "ComfyUI", "models", "checkpoints", "ace_step_v1_3.5b.safetensors"))


def sfx_items(slug):
    """One row per sound this project's scripts actually ask for.

    Driven by the scripts rather than by the vocabulary, so the queue is the
    dozen sounds this novel needs and not all forty-seven.
    """
    base = os.path.join(PROJECTS, slug)
    used, order = {}, []
    sdir = os.path.join(base, "script")

    def note(name):
        c = foley.canon(name)
        if not c:
            return
        if c not in used:
            used[c] = 0
            order.append(c)
        used[c] += 1

    if os.path.isdir(sdir):
        for f in sorted(os.listdir(sdir)):
            if not re.match(r"^\d\d\.json$", f):
                continue
            for sh in (_read_json(os.path.join(sdir, f), {}) or {}).get("shots", []):
                for b in shot_beats(sh):
                    for nm in b["sfx"]:
                        note(nm)
                snd = sh.get("sound") or {}
                if isinstance(snd, dict):
                    for fld in ("ambience", "foley", "sfx"):
                        for nm in foley.normalize(snd.get(fld)):
                            note(nm)
                # the swing fightcut puts in front of every hit it finds
                if fightcut.FIGHT_GRADE and is_combat(sh):
                    note("破风")
    bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
    prof = look.profile(bp)
    rows = []
    for nm in order:
        sp = foley.SFX[nm]
        p = sfx_file(nm, bp)
        rows.append({"id": nm, "file": sp["file"], "kind": sp["kind"],
                     "shots": used[nm],
                     # Shown as they will be sent - the Ming door, not
                     # "{door}", and the subtraction as well: on this model it
                     # is read rather than ignored, so it is part of the
                     # prompt a person is checking.
                     "tags": look.foley_tags(foley.tags_for(sp), bp),
                     "negative": _sfx_negative(sp, bp), "exists": bool(p),
                     "hand": bool(p and p.endswith(".wav")) and sp["file"] not in SFX_SYNTH,
                     "synth": sp["file"] in SFX_SYNTH,
                     "seconds": round(_duration(p), 2) if p else 0,
                     "url": ("/media/_sfx/%s/%s?t=%d"
                             % (prof, os.path.basename(p),
                                int(os.path.getmtime(p)))) if p else ""})
    return rows


def _sound_events(path, noise="-32dB", hold=0.12, span=None):
    """[(start, end)] of the non-silent runs - silencedetect, inverted.

    `span` restricts it to one stretch of the file and returns times relative
    to that stretch's start; asetpts is what makes silencedetect count from
    zero again after the trim.
    """
    dur = _duration(path)
    exe = _ffmpeg()
    if not exe or dur <= 0:
        return [], 0.0
    af = "silencedetect=noise=%s:d=%.2f" % (noise, hold)
    if span:
        dur = max(0.0, span[1] - span[0])
        af = "atrim=%.3f:%.3f,asetpts=N/SR/TB," % (span[0], span[1]) + af
    try:
        pr = subprocess.run([exe, "-hide_banner", "-i", path, "-af", af,
                             "-f", "null", "-"], capture_output=True, timeout=120)
    except Exception:                                       # noqa: BLE001
        return [], dur
    err = pr.stderr.decode("utf-8", "replace")
    starts = [float(v) for v in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(v) for v in re.findall(r"silence_end: (-?[\d.]+)", err)]
    quiet = []
    for i, s in enumerate(starts):
        quiet.append((max(0.0, s), ends[i] if i < len(ends) else dur))
    loud, t = [], 0.0
    for s, e in quiet:
        if s - t > 0.05:
            loud.append((t, s))
        t = e
    if dur - t > 0.05:
        loud.append((t, dur))
    return loud, dur


def _peak_db(path, start, end):
    """max_volume over one span, off ffmpeg's volumedetect."""
    exe = _ffmpeg()
    if not exe:
        return -99.0
    try:
        pr = subprocess.run([exe, "-hide_banner", "-i", path, "-af",
                             "atrim=%.3f:%.3f,volumedetect" % (start, end),
                             "-f", "null", "-"], capture_output=True, timeout=120)
    except Exception:                                       # noqa: BLE001
        return -99.0
    m = re.search(r"max_volume: (-?[\d.]+) dB",
                  pr.stderr.decode("utf-8", "replace"))
    return float(m.group(1)) if m else -99.0


# Stable Audio lays a full-scale CLICK on the first frame of almost every
# generation. Measured 2026-09-17: a 5 s door came back with the whole file
# peaking at 0.0 dB entirely because of a 0.19 s transient at t=0, while the
# door itself sat at -27 to -31 dB. Left in, that click wins the loudest-event
# contest every time and the library fills up with pops.
SFX_HEAD_TRIM = 0.25
# …and the real content being at -30 dB is why the floor cannot be a constant.
# A fixed -32 dB threshold found nothing at all in that take. The floor is set
# relative to what the take actually peaks at, so it adapts to whatever level
# the model felt like producing.
SFX_FLOOR_BELOW_PEAK = 25.0
SFX_TARGET_DBFS = -4.0


def _normalize_to(src, dst, target=SFX_TARGET_DBFS, extra=""):
    """Write src to dst at `target` dBFS peak, measuring first.

    Two passes rather than one, because a single pass has to guess the gain
    from a measurement of the UNTRIMMED source and then cap it - which is how
    a correctly-cut door ended up 19 dB quiet. Measuring the thing actually
    being written is exact and costs one more ffmpeg call on a file under
    three seconds long.
    """
    peak = _peak_db(src, 0.0, _duration(src))
    if peak <= -90.0:
        return 0.0
    af = "volume=%.2fdB" % (target - peak)
    if extra:
        af += "," + extra
    _ff(["-i", src, "-af", af, "-c:a", "libmp3lame", "-q:a", "2", dst], timeout=300)
    return _duration(dst)


def sfx_extract(src, dst, kind):
    """A generated take -> one usable sound file.

    A LOOP is kept whole and levelled; it is a texture and its whole length is
    the point. A ONE-SHOT is cut down to its loudest single event, because
    what the generator returns is several events with silence between them and
    a clip that plays all of them is a door opening four times - the very
    thing the beat prompts are there to stop.

    Returns the finished length, or 0.0 when there was no event to cut - which
    is the model having written a sustained pad instead of a sound, and is the
    caller's cue to re-roll.
    """
    dur = _duration(src)
    head = SFX_HEAD_TRIM if dur > SFX_HEAD_TRIM + 0.3 else 0.0
    if dur <= 0.3:
        return 0.0
    body = _peak_db(src, head, dur)
    if body < -55.0:
        return 0.0                       # silence, not a sound
    work = dst + ".cut.mp3"
    try:
        if kind == "loop":
            _ff(["-i", src, "-af", "atrim=%.3f:%.3f,asetpts=N/SR/TB" % (head, dur),
                 "-c:a", "libmp3lame", "-q:a", "2", work], timeout=300)
            got = _normalize_to(work, dst)
            return got
        floor = max(-60.0, body - SFX_FLOOR_BELOW_PEAK)
        loud, _bodydur = _sound_events(src, noise="%.1fdB" % floor, hold=0.05,
                                       span=(head, dur))
        if not loud:
            return 0.0
        # Times come back relative to the head trim; put them back on the
        # source's own clock before anything is cut.
        loud = [(head + a, head + b) for a, b in loud]
        s, e = max(loud, key=lambda r: _peak_db(src, r[0], r[1]))
        peak = _peak_db(src, s, e)
        # Where the generator lays continuous hiss under the sound, the whole
        # take reads as ONE event and the cut below just hits its ceiling -
        # every one-shot came out at exactly 2.53 s, which is a ceiling, not a
        # sound. Search the loud stretch again at a floor under its OWN peak.
        if e - s > 1.2:
            core, _ = _sound_events(src, noise="%.1fdB" % (peak - 14.0),
                                    hold=0.05, span=(s, e))
            if core:
                cs, ce = max(core, key=lambda r: r[1] - r[0])
                s, e = s + cs, s + ce
        # A little before the transient so the attack survives, a little after
        # so the decay does; capped, because an "event" that runs three
        # seconds is the pad case wearing a different hat.
        s = max(head, s - 0.05)
        e = min(dur, max(s + 0.25, min(e + 0.30, s + 2.5)))
        _ff(["-i", src, "-af",
             "atrim=%.3f:%.3f,asetpts=N/SR/TB" % (s, e),
             "-c:a", "libmp3lame", "-q:a", "2", work], timeout=300)
        return _normalize_to(
            work, dst,
            extra="afade=t=in:st=0:d=0.01,afade=t=out:st=%.3f:d=0.08"
                  % max(0.01, (e - s) - 0.08))
    finally:
        try:
            os.unlink(work)
        except OSError:
            pass


class SfxBatch:
    """Generate the missing sounds, one at a time, in the server.

    Fourth queue, same shape as the other three - state on disk, resumes after
    a restart, stops between items, retries rather than dying when ComfyUI is
    away. It shares ComfyUI's queue with the video sampler like the music does,
    so a sound generated while a chapter renders costs that chapter the ~20 s
    it takes; a dozen of them before the render costs nothing at all.
    """

    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "sfx_batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.state = _read_json(self.path, None) or {
            "status": "idle", "queue": [], "done": [], "failed": []}

    _save = VideoBatch._save
    snapshot = VideoBatch.snapshot
    _log = VideoBatch._log
    _comfy = Batch._comfy
    start = MusicBatch.start
    resume = MusicBatch.resume
    stop = MusicBatch.stop
    _run = MusicBatch._run

    def _one(self, name):
        sp = foley.spec(name)
        if not sp:
            raise RuntimeError("音效表里没有 %s" % name)
        bp = _read_json(os.path.join(PROJECTS, self.slug, "blueprint.json"), {}) or {}
        out = sfx_dir(bp)
        # The fight and crowd sounds Stable Audio could not make are BUILT by
        # scripts/make-fight-foley.py; 重出 on one of them builds it again on
        # a new seed, never falls back to the model that failed at it.
        if sp["file"] in SFX_SYNTH:
            return self._synth(name, sp, out)
        if not _sfx_model_present():
            raise RuntimeError(
                "音效模型还没就位：需要 ComfyUI/models/checkpoints/%s 和 "
                "ComfyUI/models/text_encoders/%s" % (SFX_CKPT, SFX_CLIP))
        # 重出 on anyone else's .wav used to be refused (已锁定, 2026-09-30:
        # "取消已锁定，允许重新生成"). The recording is MOVED to sfx/_trash,
        # never deleted - it is the one thing here that cannot be regenerated.
        if os.path.isfile(os.path.join(out, sp["file"] + ".wav")):
            gone = _trash_takes(out, sp["file"])
            self._log("· %s 的手工录音移到 %s，改用生成的" % (name, os.path.relpath(gone, ROOT)))
        # The description is built for THIS world, not for doors in general:
        # {door} becomes a Ming courtyard leaf on a wooden pivot or a modern
        # slab on steel hinges depending on the blueprint. See look.materials.
        tags = look.foley_tags(foley.tags_for(sp), bp)
        neg = _sfx_negative(sp, bp)
        os.makedirs(out, exist_ok=True)
        dst = os.path.join(out, sp["file"] + ".mp3")
        last = ""
        for attempt in range(SFX_RETRIES + 1):
            raw = self._generate(name, sp, attempt, tags, neg)
            tmp = dst + ".part.mp3"
            got = sfx_extract(raw, tmp, sp["kind"])
            try:
                os.unlink(raw)
            except OSError:
                pass
            if got > 0.05:
                os.replace(tmp, dst)
                self._log("✓ %s（%s）%.2f 秒" % (name, sp["file"], got))
                return
            for q in (tmp,):
                try:
                    os.unlink(q)
                except OSError:
                    pass
            last = "第 %d 次出来的是一段持续音，里面没有一次独立的响声" % (attempt + 1)
            self._log("· %s %s，换种子重来" % (name, last))
        raise RuntimeError(last or "没有生成出可用的音效")

    def _synth(self, name, sp, out):
        py = previz_py()
        if not py:
            raise RuntimeError("venvs/tools 没有 python（合成音效要 numpy + scipy）")
        key = SFX_SYNTH[sp["file"]]
        tmp = os.path.join(SFX_DIR, "_build", "%s-%d" % (sp["file"], int(time.time())))
        os.makedirs(tmp, exist_ok=True)
        cmd = [py, os.path.join(ROOT, "scripts", "make-fight-foley.py"), tmp,
               "--only", key, "--seed", str(random.getrandbits(31))]
        if key in ("shout", "cheer", "murmur", "remark"):
            cmd.append("--cached")      # CosyVoice cannot load beside a render
        self._log("▶ %s（%s）合成，新种子" % (name, sp["file"]))
        try:
            r = subprocess.run(["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"']
                               + cmd, capture_output=True, text=True, timeout=1800)
            new = sorted(glob.glob(os.path.join(tmp, glob.escape(sp["file"]) + "*.wav")))
            if r.returncode != 0 or not new:
                raise RuntimeError("合成失败：" + ((r.stderr or r.stdout or "").strip().splitlines() or ["没有输出"])[-1][:200])
            # swap only once the new takes exist: a failed build keeps the old sound
            if glob.glob(os.path.join(out, glob.escape(sp["file"]) + "*.wav")):
                _trash_takes(out, sp["file"])
            os.makedirs(out, exist_ok=True)
            for f in new:
                shutil.move(f, os.path.join(out, os.path.basename(f)))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._log("✓ %s（%s）%d 条" % (name, sp["file"], len(new)))

    def _generate(self, name, sp, attempt, tags, neg=""):
        """One Stable Audio render -> a temp file. Returns its path."""
        wf = _read_json(SFX_WORKFLOW, None)
        if not wf:
            raise RuntimeError("读不到音效工作流 %s" % SFX_WORKFLOW)
        wf.pop("_comment", None)
        wf["2"]["inputs"]["text"] = tags
        wf["3"]["inputs"]["text"] = neg or SFX_NEGATIVE
        # The model is conditioned on how long the sound is, so node 4 and
        # node 5 must agree - disagreeing values give a clip padded to length
        # with silence.
        secs = SFX_LOOP_SECONDS if sp["kind"] == "loop" else SFX_SECONDS
        wf["4"]["inputs"]["seconds_total"] = secs
        wf["5"]["inputs"]["seconds"] = secs
        wf["6"]["inputs"]["seed"] = random.getrandbits(32)
        wf["6"]["inputs"]["steps"] = SFX_STEPS
        wf["6"]["inputs"]["cfg"] = SFX_CFG
        wf["8"]["inputs"]["filename_prefix"] = "bookreel/sfx-%s" % sp["file"]
        left = [k for k in ("__TAGS__", "__NEGATIVE__")
                if k in json.dumps(wf, ensure_ascii=False)]
        if left:
            raise RuntimeError("工作流里残留占位符：%s" % "、".join(left))
        if attempt == 0:
            self._log("▶ %s（%s，%s，%.0f 秒）%s"
                      % (name, sp["file"], sp["kind"], secs, tags[:100]))

        pid = None
        while pid is None:
            if self.stop_flag:
                raise RuntimeError("停止")
            try:
                pid = self._comfy("/prompt", {"prompt": wf, "client_id": BATCH_CLIENT})["prompt_id"]
            except Exception as exc:                        # noqa: BLE001
                with self.lock:
                    self.state["last_error"] = "ComfyUI 不可达，30 秒后重试：%s" % str(exc)[:120]
                    self._save()
                time.sleep(30)
        with self.lock:
            self.state["last_error"] = ""
            self._save()

        t0 = time.time()
        while True:
            time.sleep(3)
            if self.stop_flag:
                raise RuntimeError("停止")
            if time.time() - t0 > SFX_STALL:
                raise RuntimeError("%s 等了 %d 分钟没有结果" % (name, SFX_STALL // 60))
            try:
                rec = self._comfy("/history/" + pid, timeout=30).get(pid)
            except Exception:                               # noqa: BLE001
                continue
            if not rec:
                continue
            stx = rec.get("status", {})
            if stx.get("status_str") == "error":
                raise RuntimeError("ComfyUI 任务失败：%s"
                                   % json.dumps(stx.get("messages", [])[-1:], ensure_ascii=False)[:200])
            for out in rec.get("outputs", {}).values():
                for au in (out.get("audio") or []):
                    url = "%s/view?filename=%s&subfolder=%s&type=%s" % (
                        COMFY, urllib.parse.quote(au["filename"]),
                        urllib.parse.quote(au.get("subfolder", "")), au.get("type", "output"))
                    with urllib.request.urlopen(url, timeout=300) as r:
                        body = r.read()
                    if not body:
                        raise RuntimeError("取到 0 字节")
                    os.makedirs(SFX_DIR, exist_ok=True)
                    raw = os.path.join(SFX_DIR, "_raw-%s.mp3" % sp["file"])
                    with open(raw, "wb") as f:
                        f.write(body)
                    return raw
            if rec.get("outputs"):
                raise RuntimeError("任务完成但没有音频输出")


_SFX = {}


def sfx_batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _SFX:
            _SFX[slug] = SfxBatch(slug)
        return _SFX[slug]


_MUSIC = {}


def music_batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _MUSIC:
            _MUSIC[slug] = MusicBatch(slug)
        return _MUSIC[slug]


# ----------------------------------------------------------------------
# lip-sync pass. LatentSync 1.6 in venvs/lipsync (services/lipsync.py).
# ----------------------------------------------------------------------
# MiniMax-H3 has no audio conditioning: it draws the mouth from text alone, so
# no prompt can carry phonemes, and the lips of every dialog shot were wrong by
# construction. Everything before this narrowed the damage (a closed mouth
# under the narrator, sound delayed on a cold start); this is the first thing
# that produces SYNC, by redrawing the mouth of the finished clip to its own
# dialog audio. 38 shots in the book carry dialog, 32 of them MCU or tighter.
#
# A separate queue, for the 4K pass's reason: 18 GB of VRAM and a diffusion
# loop want the GPU the sampler is using, and welded into _render a failed
# sync would stall every shot behind it. The result is published as a NEW
# TAKE of the clip, so the previous take stays on disk, the film rebuilds
# because a clip got newer, and the 4K pass redoes it for the same reason -
# nothing downstream has to know this pass exists.
#
# The mouth is driven by the DIALOG ONLY. The clip's track is narrator + lines
# + foley; handed all of it, the model would mouth the narration, which is the
# voice-over defect in a new coat. So the driving wav is the clip's own audio
# with everything outside the dialog cues silenced - the cues are in the take
# sidecar, measured off the wav that went into the clip.
# Hands-off: a dialog shot goes onto the lip-sync queue the moment its clip
# lands, the way it goes to the lip check. The two queues then share the GPU
# for a few minutes per dialog shot - 38 in the book - which is the price of
# not having to remember to press a button after every chapter.
LIPSYNC_AUTO = os.environ.get("BOOKREEL_LIPSYNC_AUTO", "1") not in ("0", "", "false")
LIPSYNC_STEPS = int(os.environ.get("BOOKREEL_LIPSYNC_STEPS", "20"))
LIPSYNC_GUIDANCE = float(os.environ.get("BOOKREEL_LIPSYNC_GUIDANCE", "1.5"))
LIPSYNC_STALL = int(os.environ.get("BOOKREEL_LIPSYNC_STALL", str(90 * 60)))
# It runs in a window with ComfyUI STOPPED. Measured 2026-09-18: beside a
# render, 582 s per 16-frame chunk and ComfyUI went down on the first attempt;
# alone, with the SDPA flag, 41.8 s a chunk. Comfy holds 24 GB of this box's
# 31 GB host memory and LatentSync wants ~5 GB plus 18 GB of VRAM, which is
# the same arithmetic that keeps text generation off a live comfy. So the
# queue waits for the render queue to be idle, stops comfy, does every shot
# it has, and brings comfy back - a render started meanwhile gets 503 from h3
# and retries until it is.
LIPSYNC_WAIT_POLL = 60


def _lipsync_py():
    for c in (os.path.join(ROOT, "venvs", "lipsync", "bin", "python"),
              os.path.join(ROOT, "venvs", "lipsync", "Scripts", "python.exe")):
        if os.path.isfile(c):
            return c
    return None


def _dialog_windows(shot, meta):
    """[(start, end)] of the cues that are LINES, not narration."""
    lines = [str(d.get("line") or "").strip() for d in spoken(shot)]
    lines = [l for l in lines if l]
    out = []
    for c in (meta.get("cues") or []):
        txt = str(c.get("text") or "").strip().strip("，。！？、；：\"“”")
        if txt and any(txt in l or l in txt for l in lines):
            out.append((float(c["start"]), float(c["end"])))
    return out


LIPSYNC_MIN_FACE = float(os.environ.get("BOOKREEL_LIPSYNC_MIN_FACE", "0.10"))


def _mouth_trace(clip, fps=6.0):
    """scripts/mouth-trace.py over a clip: where every face is, and how open
    its mouth is, a few times a second. None when the venv or script is
    missing. ~1 s of CPU per second of clip."""
    py = _lipsync_py()
    script = os.path.join(ROOT, "scripts", "mouth-trace.py")
    if not py or not os.path.isfile(script) or not os.path.isfile(clip):
        return None
    try:
        pr = subprocess.run([py, script, clip, "%g" % fps], cwd=ROOT, capture_output=True, timeout=900)
        out = pr.stdout.decode("utf-8", "replace")
        # The JSON is the LAST thing on stdout. insightface printed its model
        # loading in front of it and this parse failed on all eleven dialog
        # shots of tianlongbabu ch01 (2026-10-07): no face hint, no verdict,
        # ~6 GPU-hours blind for the second time. The script sends that
        # chatter to stderr now, and this side takes the object wherever it
        # starts rather than trusting the stream to be clean.
        k = out.rfind('{"fps"')
        if k < 0:
            raise ValueError("mouth-trace printed no JSON: %s" % out[-200:])
        return json.loads(out[k:])
    except Exception as exc:                                # noqa: BLE001
        print("[web] mouth-trace failed on %s: %s" % (os.path.basename(clip), str(exc)[:200]), flush=True)
        return None


def _face_tracks(trace, bucket=0.12):
    """Faces clustered by where they stand: [{x, y, h, n, open: [(t, o)]}],
    biggest first. A face is the same face when its centre is within
    `bucket` of the frame's width of the track's."""
    tracks = []
    for t, faces in ((trace or {}).get("frames") or []):
        for cx, cy, h, o in faces:
            tr = next((k for k in tracks if abs(k["x"] - cx) < bucket and abs(k["y"] - cy) < bucket * 1.5), None)
            if tr is None:
                tr = {"x": cx, "y": cy, "h": h, "n": 0, "open": []}
                tracks.append(tr)
            k = tr["n"]
            tr["x"] = (tr["x"] * k + cx) / (k + 1)
            tr["y"] = (tr["y"] * k + cy) / (k + 1)
            tr["h"] = (tr["h"] * k + h) / (k + 1)
            tr["n"] = k + 1
            tr["open"].append((t, o))
    return sorted(tracks, key=lambda k: -k["h"] * k["n"])


def _wav_rms(path, hop):
    """[(t, rms)] of a mono 16-bit wav every `hop` seconds, pure python - a
    driving wav is 16 kHz and a few hundred thousand samples."""
    import wave
    out = []
    with wave.open(path, "rb") as w:
        sr, nch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        n = w.getnframes()
        raw = w.readframes(n)
    if sw != 2:
        return out
    import array
    a = array.array("h")
    a.frombytes(raw[:len(raw) - len(raw) % 2])
    if nch > 1:
        a = a[::nch]
    step = max(1, int(sr * hop))
    for i in range(0, len(a), step):
        seg = a[i:i + step]
        if len(seg) < 32:
            break
        out.append((i / float(sr), math.sqrt(sum(float(v) * v for v in seg) / len(seg)) / 32768.0))
    return out


def _mouth_sync(track, drive, windows):
    """How much a face's mouth FOLLOWS the voice: the correlation between the
    lip extent and the driving wav's loudness, sampled inside the lines. A
    mouth h3 animates on its own sits near 0 (measured over every dialog
    shot of tianlongbabu ch01, 2026-10-09: -0.1 … 0.15, before and after a
    blind sync); a mouth that opens on the syllables is clearly positive.
    The spread (`_mouth_motion`) alone cannot tell those apart - h3's own
    random mouthing has as much spread as a synced one, sometimes more."""
    try:
        pts = [(t, o) for t, o in (track.get("open") or [])
               if any(a - 0.1 <= t <= b + 0.1 for a, b in windows)]
        if len(pts) < 8 or not drive or not os.path.isfile(drive):
            return None
        hop = 1.0 / 12.0
        env = _wav_rms(drive, hop)
        if not env:
            return None
        xs, ys = [], []
        for t, o in pts:
            k = int(t / hop)
            if 0 <= k < len(env):
                xs.append(float(o))
                ys.append(env[k][1])
        n = len(xs)
        if n < 8:
            return None
        mx, my = sum(xs) / n, sum(ys) / n
        sx = math.sqrt(sum((x - mx) ** 2 for x in xs)) or 0.0
        sy = math.sqrt(sum((y - my) ** 2 for y in ys)) or 0.0
        if sx < 1e-9 or sy < 1e-9:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)
    except Exception:                                       # noqa: BLE001
        return None


def _mouth_motion(track, windows):
    """How much a track's mouth moves inside the dialog windows: the spread
    (max-min) of the lip extent, which stays about 0.01 on a closed mouth and
    reaches 0.1 and more on one that talks."""
    xs = [o for t, o in track.get("open") or [] if any(a - 0.1 <= t <= b + 0.1 for a, b in windows)]
    return (max(xs) - min(xs)) if len(xs) >= 3 else 0.0


def _speaker_track(clip, shot, bp, windows, tracks):
    """Which face in the clip is the one that speaks, or None.

    LatentSync syncs the LARGEST face, and in a two-shot that is whoever is
    nearer the lens, not whoever talks (C01S13/14: 龚光杰 and 褚 the same size,
    one line each). One face is the answer; with more, the vision model is
    asked where the person in the speaker's clothes stands - the clothes are
    in the blueprint and a colour is something it reads reliably, where "who
    is talking" is not when h3 drew nobody's mouth moving - and the nearest
    track by x is taken. Nothing to go on: the biggest, as before."""
    tracks = [k for k in tracks if k["n"] >= 3]
    if not tracks:
        return None
    if len(tracks) == 1:
        return tracks[0]
    who = [str(d.get("who")) for d in spoken(shot) if d.get("who")]
    by_id = {str(c.get("id")): c for c in ((bp or {}).get("characters") or []) if c.get("id")}
    c = by_id.get(who[0]) if who else None
    cos = ""
    if c:
        cos = re.split(r"[，。;；,]", re.sub(r"[（(][^）)]*[）)]", "",
                                             filmwords.strip_weapons(_plain(c.get("costume")) or "")))[0].strip()
    if cos and len(cos) <= 24 and windows:
        try:
            a, b = windows[0]
            png = _grab(clip, (a + b) / 2.0)
            ans = _vlm_json("画面里穿%s的人站在画面的左边、中间还是右边？"
                            '只输出 {"位置":"左边"}、{"位置":"中间"} 或 {"位置":"右边"}。' % cos,
                            png, prefill='{"位置":')
            v = str((ans or {}).get("位置", ""))
            x = 0.22 if "左" in v else (0.78 if "右" in v else (0.5 if "中" in v else None))
            if x is not None:
                return min(tracks, key=lambda k: abs(k["x"] - x))
        except Exception:                                   # noqa: BLE001
            pass
    # the mouth that moves most during the line, if any does
    moved = sorted(tracks, key=lambda k: -_mouth_motion(k, windows))
    if _mouth_motion(moved[0], windows) >= 0.06 and (
            len(moved) == 1 or _mouth_motion(moved[0], windows) > 1.5 * _mouth_motion(moved[1], windows)):
        return moved[0]
    return tracks[0]


def _drive_audio(clip, windows, out):
    """The clip's own audio with everything outside the dialog silenced."""
    keep = "+".join("between(t,%.3f,%.3f)" % (max(0.0, a - 0.05), b + 0.05) for a, b in windows)
    _ff(["-i", clip, "-vn", "-af", "volume=0:enable='not(%s)'" % keep,
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", out], timeout=120)
    return out


def _lipsync_keys(slug, redo=False):
    """Every dialog shot with a clip on disk whose live take is not yet synced."""
    base = os.path.join(PROJECTS, slug)
    keys = []
    for f in sorted(glob.glob(os.path.join(base, "script", "[0-9][0-9].json"))):
        sc = _read_json(f, {}) or {}
        n = int(sc.get("n") or os.path.basename(f)[:2])
        for sh in (sc.get("shots") or []):
            sid = sh.get("id")
            if not sid or not spoken(sh):
                continue
            clip = os.path.join(base, "clips", "%02d" % n, sid + ".mp4")
            if not os.path.isfile(clip):
                continue
            takes = os.path.join(base, "clips", "%02d" % n, "_takes", sid)
            t = (_read_json(os.path.join(takes, "_active.json"), {}) or {}).get("take")
            meta = _read_json(os.path.join(takes, "%s.json" % t), {}) or {}
            if meta.get("lipsync") and not redo:
                continue
            if meta.get("approved"):
                continue
            keys.append("%d:%s" % (n, sid))
    return keys


def clip_approved(slug, key):
    """Has the live take of this clip been accepted by hand (人工认可)?"""
    try:
        n, sid = str(key).split(":", 1)
    except ValueError:
        return False
    takes = os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), "_takes", sid)
    t = (_read_json(os.path.join(takes, "_active.json"), {}) or {}).get("take")
    return bool((_read_json(os.path.join(takes, "%s.json" % t), {}) or {}).get("approved"))


class LipsyncBatch:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "lipsync_batch.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.proc = None
        self.state = _read_json(self.path, None) or {
            "status": "idle", "queue": [], "done": [], "failed": []}

    _save = VideoBatch._save
    snapshot = VideoBatch.snapshot
    _log = VideoBatch._log

    def start(self, keys, front=False, redo=False):
        keys = [k for k in keys if not clip_approved(self.slug, k)]
        if not keys:
            return {"ok": True, "queued": len(self.state.get("queue") or []), "approved_skipped": True}
        with self.lock:
            if self.thread and self.thread.is_alive():
                have = set(self.state["queue"]) | {self.state.get("current") or ""}
                fresh = [k for k in keys if k not in have]
                if front:
                    self.state["queue"][0:0] = fresh
                else:
                    self.state["queue"].extend(fresh)
                self.state["redo"] = bool(self.state.get("redo") or redo)
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"])}
            self.state = {"status": "running", "queue": list(keys), "done": [], "failed": [],
                          "started": int(time.time()), "current": None, "current_started": None,
                          "last_error": "", "log": [], "redo": bool(redo)}
            self._save()
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="lipsync-" + self.slug, daemon=True)
            self.thread.start()
            return {"ok": True, "queued": len(keys)}

    def resume(self):
        st = self.state
        if st.get("status") == "running" and (st.get("queue") or st.get("current")):
            if st.get("current") and st["current"] not in st["queue"]:
                st["queue"].insert(0, st["current"])
            st["current"] = None
            self._log("服务重启，续跑口型同步：剩余 %d 镜" % len(st["queue"]))
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="lipsync-" + self.slug, daemon=True)
            self.thread.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        self._log("收到停止：当前这镜同步完就停")
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _render_busy(self):
        vb = _VIDEO.get(self.slug)
        return bool(vb and (vb.state or {}).get("status") == "running")

    def _comfy_up(self):
        try:
            urllib.request.urlopen(COMFY + "/queue", timeout=5).read()
            return True
        except Exception:                                   # noqa: BLE001
            return False

    def _run(self):
        stopped_comfy = False
        waited = False
        try:
            while True:
                with self.lock:
                    if self.stop_flag or not self.state["queue"]:
                        self.state["status"] = "stopped" if self.stop_flag else (
                            "done" if not self.state["failed"] else "done_with_failures")
                        self.state["current"] = None
                        self.state["finished"] = int(time.time())
                        self._save()
                        return
                # The window: render idle, comfy down.
                if self._render_busy():
                    if not waited:
                        self._log("等出片队列空下来再同步口型（和采样器不能同时占这台机器）")
                        waited = True
                    time.sleep(LIPSYNC_WAIT_POLL)
                    continue
                waited = False
                if self._comfy_up():
                    self._log("停 ComfyUI，让出主机内存给 LatentSync")
                    subprocess.run([os.path.join(ROOT, "launch.sh"), "comfy-stop"],
                                   capture_output=True, timeout=600)
                    stopped_comfy = True
                with self.lock:
                    key = self.state["queue"].pop(0)
                    self.state["current"] = key
                    self.state["current_started"] = int(time.time())
                    self._save()
                try:
                    self._one(key)
                    with self.lock:
                        self.state["done"].append(key)
                        self._save()
                except Exception as exc:                    # noqa: BLE001
                    with self.lock:
                        self.state["failed"].append(key)
                        self.state["last_error"] = str(exc)[:300]
                        self._save()
                    self._log("！%s 口型同步失败：%s" % (key, str(exc)[:160]))
        finally:
            if stopped_comfy:
                self._log("口型同步窗口结束，起回 ComfyUI")
                try:
                    subprocess.run([os.path.join(ROOT, "launch.sh"), "comfy-restart"],
                                   capture_output=True, timeout=1200)
                except Exception as exc:                    # noqa: BLE001
                    self._log("！ComfyUI 没起回来：%s" % str(exc)[:120])

    def _one(self, key):
        n, sid = key.split(":", 1)
        n = int(n)
        base = os.path.join(PROJECTS, self.slug)
        sc = _read_json(os.path.join(base, "script", "%02d.json" % n), {}) or {}
        shot = next((x for x in (sc.get("shots") or []) if x.get("id") == sid), None)
        if not shot:
            raise RuntimeError("script 里没有 %s" % sid)
        bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
        clip = os.path.join(base, "clips", "%02d" % n, sid + ".mp4")
        if not os.path.isfile(clip):
            raise RuntimeError("还没有这镜的成片")
        py = _lipsync_py()
        if not py:
            raise RuntimeError("venvs/lipsync 不存在（LatentSync 没装）")
        takes = os.path.join(base, "clips", "%02d" % n, "_takes", sid)
        take = (_read_json(os.path.join(takes, "_active.json"), {}) or {}).get("take")
        meta = _read_json(os.path.join(takes, "%s.json" % take), {}) or {}
        # `src` is what LatentSync reads; `clip` stays the live path the new
        # take is published to. A synced take is never synced AGAIN - a mouth
        # redrawn from a redrawn mouth - so a 重出 (`redo`) goes back to the
        # take the sync came from (`lipsync_from`, still on disk: deleting is
        # moving) and publishes a fresh take beside the old one.
        src = clip
        if meta.get("lipsync"):
            frm = meta.get("lipsync_from")
            src = os.path.join(takes, "%s.mp4" % frm)
            if not self.state.get("redo") or not frm or not os.path.isfile(src):
                self._log("· %s 这一个 take 已经同步过，跳过" % key)
                return
            meta = _read_json(os.path.join(takes, "%s.json" % frm), {}) or meta
            take = frm
            self._log("↻ %s 重做口型同步：从第 %s 次的原片重来" % (key, frm))
        windows = _dialog_windows(shot, meta)
        if not windows:
            self._log("· %s 没有台词时间轴，跳过" % key)
            return
        work = os.path.join(base, "clips", "%02d" % n, "_frames")
        os.makedirs(work, exist_ok=True)
        # WHOSE mouth, and is it big enough to draw. The pass ran on all
        # eleven dialog shots of tianlongbabu ch01 (2026-10-06) and changed
        # none of them visibly - 6 GPU-hours - partly because the face it
        # chose was the largest, not the speaker's, and partly because a
        # 512 px model redrawing a face 40 px tall has nothing to work with.
        t0 = time.time()
        trace = _mouth_trace(src, fps=12.0)
        tracks = _face_tracks(trace)
        spk = _speaker_track(src, shot, bp, windows, tracks)
        env = dict(os.environ)
        before = None
        if spk is None:
            # Said out loud: this is exactly the state the pass ran in on
            # 2026-10-06 AND 2026-10-07 without a word in the log - the tracer
            # had failed, LatentSync took the largest face, and nothing could
            # measure what came back.
            self._log("⚠ %s 没量到说话的脸（追踪器%s），LatentSync 只会同步最大的那张脸，结果无法核验"
                      % (key, "没有输出" if trace is None else "找到 %d 条轨迹" % len(tracks)))
        if spk:
            before = _mouth_motion(spk, windows)
            env["LATENTSYNC_FACE_HINT"] = "%.3f,%.3f" % (spk["x"], spk["y"])
            if spk["h"] < LIPSYNC_MIN_FACE:
                _take_note(clip, take, {"lipsync": "skipped",
                                        "lipsync_skipped": "说话的人的脸只有画面高度的 %.0f%%，太小，不同步" % (spk["h"] * 100)})
                self._log("· %s 说话的脸太小（%.0f%% 画高），不做口型同步" % (key, spk["h"] * 100))
                return
            self._log("   %s 说话的人在 x=%.2f，脸高 %.0f%%，台词内嘴动 %.3f（%d 张脸）"
                      % (key, spk["x"], spk["h"] * 100, before, len(tracks)))
        drive = _drive_audio(src, windows, os.path.join(work, "%s-drive.wav" % sid))
        sync0 = _mouth_sync(spk, drive, windows) if spk else None
        raw = os.path.join(work, "%s-lipsync-raw.mp4" % sid)
        self._log("▶ %s 口型同步：%d 句台词，%.1f 秒有声" % (key, len(windows), sum(b - a for a, b in windows)))
        argv = ["sh", "-c", 'echo 1000 > /proc/self/oom_score_adj 2>/dev/null; exec "$0" "$@"',
                py, os.path.join(ROOT, "services", "lipsync.py"),
                "--clip", src, "--audio", drive, "--out", raw,
                "--steps", str(LIPSYNC_STEPS), "--guidance", "%g" % LIPSYNC_GUIDANCE]
        self.proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            out, _ = self.proc.communicate(timeout=LIPSYNC_STALL)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            raise RuntimeError("超过 %d 分钟没出结果，已杀掉" % (LIPSYNC_STALL // 60))
        finally:
            rc = self.proc.returncode
            self.proc = None
        if rc != 0 or not os.path.isfile(raw):
            tail = out.decode("utf-8", "replace").strip().splitlines()[-6:]
            raise RuntimeError("LatentSync 退出 %s：%s" % (rc, " | ".join(tail)[-300:]))
        # Back to the clip's own parameters, with the clip's own MIX. The model
        # wrote the driving wav as the track; the narrator, the foley and the
        # room are in the original and are copied back untouched.
        params = _stream_params(src)
        dur = _duration(src)
        final = os.path.join(work, "%s-lipsync.mp4" % sid)
        args = ["-i", raw, "-i", src, "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "16",
                "-c:a", "copy", "-t", "%.3f" % dur, "-movflags", "+faststart"]
        if params:
            args += ["-r", "%g" % params[2]]
            if params[3]:
                args += ["-video_track_timescale", str(params[3])]
        _ff(args + [final], timeout=600)
        got = _duration(final)
        if abs(got - dur) > 0.15:
            raise RuntimeError("同步后长度 %.2f 秒，原片 %.2f 秒，不敢发布" % (got, dur))
        # Did it DO anything? Measured on the result, the way every other pass
        # here is: the speaker's mouth should move more inside the line than
        # it did before. A pass that changed nothing is published (the person
        # decides) but says so, instead of 11 silent "✓ 口型同步" over clips
        # that were pixel-identical to their source.
        after, moved, sync1 = None, None, None
        if spk is not None:
            try:
                t2 = _face_tracks(_mouth_trace(final, fps=12.0))
                near = [k for k in t2 if abs(k["x"] - spk["x"]) < 0.15 and abs(k["y"] - spk["y"]) < 0.2]
                if near:
                    after = _mouth_motion(near[0], windows)
                    moved = after - (before or 0.0)
                    sync1 = _mouth_sync(near[0], drive, windows)
            except Exception:                               # noqa: BLE001
                pass
        sync_txt = ("，口型随声音的相关 %.2f → %.2f" % (sync0, sync1)
                    if (sync0 is not None and sync1 is not None) else "")
        with open(final, "rb") as fh:
            data = fh.read()
        newmeta = dict(meta)
        newmeta.update({"lipsync": True, "lipsync_from": take, "lipsync_model": "LatentSync-1.6",
                        "lipsync_seconds": int(time.time() - t0),
                        "lipsync_face": ({"x": round(spk["x"], 3), "y": round(spk["y"], 3),
                                          "h": round(spk["h"], 3)} if spk else None),
                        "lipsync_mouth": ({"before": round(before, 3), "after": round(after, 3),
                                           "sync_before": (round(sync0, 3) if sync0 is not None else None),
                                           "sync_after": (round(sync1, 3) if sync1 is not None else None)}
                                          if after is not None else None)})
        # Two numbers, and the second is the one that matters: a mouth that
        # moves more is not a mouth that moves WITH the voice. The sync
        # correlation has to come out clearly positive, or the pass says so.
        if sync1 is not None and sync1 < 0.2:
            chk = dict(newmeta.get("check") or {})
            chk["remarks"] = list(chk.get("remarks") or []) + [
                "口型同步后嘴还是不跟声音走（相关 %.2f → %.2f，嘴动 %.3f → %.3f）"
                % (sync0 if sync0 is not None else 0.0, sync1, before or 0.0, after)]
            newmeta["check"] = chk
            self._log("⚠ %s 口型同步后嘴还是不跟声音走（相关 %.2f → %.2f，嘴动 %.3f → %.3f），照发，由你决定"
                      % (key, sync0 if sync0 is not None else 0.0, sync1, before or 0.0, after))
        elif moved is not None and moved < 0.02 and sync1 is None:
            chk = dict(newmeta.get("check") or {})
            chk["remarks"] = list(chk.get("remarks") or []) + [
                "口型同步前后嘴型几乎没变（台词内嘴动 %.3f → %.3f）" % (before or 0.0, after)]
            newmeta["check"] = chk
            self._log("⚠ %s 口型同步前后嘴型几乎没变（%.3f → %.3f），照发，由你决定" % (key, before or 0.0, after))
        elif after is not None:
            self._log("   %s 同步后台词内嘴动 %.3f → %.3f%s" % (key, before or 0.0, after, sync_txt))
        else:
            # No number is not a pass. The take says so where the person
            # looks, not only in a log line that scrolls away.
            chk = dict(newmeta.get("check") or {})
            chk["remarks"] = list(chk.get("remarks") or []) + ["口型同步没量到结果（没找到说话的脸）"]
            newmeta["check"] = chk
        nt = _take_add(clip, data, newmeta)
        for q in (raw, drive, final):
            try:
                os.unlink(q)
            except OSError:
                pass
        self._log("✓ %s 口型同步，第 %d 次（来自第 %s 次），%d 分" % (key, nt, take, (time.time() - t0) / 60.0))
        # The lip check again, on the take that is now live - and the chapter
        # film, which this clip just made stale, if the chapter is complete.
        try:
            lip = lipcheck_shot(self.slug, n, shot)
            if lip:
                lip.update({"id": sid, "at": int(time.time()), "after_lipsync": True})
                out = lip_path(self.slug, n, sid)
                with open(out, "w", encoding="utf-8") as fh:
                    json.dump(lip, fh, ensure_ascii=False)
                self._log("   %s 同步后口型核查：%s" % (key, lip.get("note") or ""))
        except Exception as exc:                            # noqa: BLE001
            self._log("   %s 同步后口型没查成：%s" % (key, str(exc)[:100]))
        try:
            video_batch_for(self.slug)._film(key)
        except Exception as exc:                            # noqa: BLE001
            self._log("   %s 成片没重合：%s" % (key, str(exc)[:100]))


def stop_everything(slug=None, hard=False):
    """Every queue down, and say what was actually running.

    There are six queues now - images, clips, lip-sync, 4K, music, foley -
    each with its own start/stop endpoint, and between them they can hold the
    GPU for days. Stopping them one at a time means knowing which ones exist,
    which is asking the person to keep this file's architecture in their head
    at the moment they least want to.

    Two strengths, because the useful ones differ:
      * graceful (default) sets every stop flag. Each queue finishes the ITEM
        in its hand and stops - a clip mid-render is 27 GPU-min a segment, and
        throwing it away to stop one shot earlier is usually the worse trade.
      * hard also interrupts what is in flight: ComfyUI's running prompt, the
        h3 task behind it, and the 4K / lip-sync subprocesses (their own stop()
        kills those). Use it when the machine is needed NOW.

    Neither touches services. Nothing is cancelled that cannot be resumed:
    every queue writes its state to disk and picks up where it stopped.
    """
    slugs = [slug] if slug else sorted(
        set(list(_BATCHES) + list(_VIDEO) + list(_LIPSYNC) + list(_UPSCALE)
            + list(_MUSIC) + list(_SFX)))
    stopped, running = [], []
    for sg in slugs:
        for name, reg in (("图片", _BATCHES), ("片段", _VIDEO), ("口型同步", _LIPSYNC),
                          ("4K", _UPSCALE), ("配乐", _MUSIC), ("音效", _SFX)):
            b = reg.get(sg)
            if b is None:
                continue
            st = b.state or {}
            if st.get("status") == "running":
                running.append({"slug": sg, "queue": name,
                                "current": st.get("current"), "left": len(st.get("queue") or [])})
            try:
                b.stop()
                stopped.append("%s/%s" % (sg, name))
            except Exception as exc:                        # noqa: BLE001
                print("[stop] %s %s: %s" % (sg, name, exc), flush=True)
    note = ""
    if hard:
        # The h3 task first: cancelling it is what makes the render loop give
        # up its poll instead of waiting out the deadline.
        for sg in slugs:
            vb = _VIDEO.get(sg)
            tid = ((vb.state or {}).get("task_id") if vb else None)
            if tid:
                try:
                    vb._cancel(tid)
                    note += " 已取消 h3 任务 %s；" % tid[:8]
                except Exception:                           # noqa: BLE001
                    pass
        try:
            urllib.request.urlopen(urllib.request.Request(
                COMFY + "/interrupt", data=b"{}",
                headers={"Content-Type": "application/json"}), timeout=10).close()
            note += " 已中断 ComfyUI 当前任务；"
        except Exception as exc:                            # noqa: BLE001
            note += " ComfyUI 中断没成功（%s）；" % str(exc)[:60]
    return {"ok": True, "hard": bool(hard), "stopped": stopped, "was_running": running,
            "note": note.strip() or ("每条队列手上这一件做完就停" if not hard else "")}


_LIPSYNC = {}


def lipsync_batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _LIPSYNC:
            _LIPSYNC[slug] = LipsyncBatch(slug)
        return _LIPSYNC[slug]


_UPSCALE = {}


def upscale_batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _UPSCALE:
            _UPSCALE[slug] = UpscaleBatch(slug)
        return _UPSCALE[slug]


def _all_4k_keys(slug, redo=False):
    """Every shot that HAS a clip; by default only those whose 4K is missing or
    older than the clip it came from."""
    out = []
    base = os.path.join(PROJECTS, slug)
    sdir = os.path.join(base, "script")
    chapters = sorted(int(f[:2]) for f in os.listdir(sdir)
                      if re.match(r"^\d\d\.json$", f)) if os.path.isdir(sdir) else []
    for n in chapters:
        sc = _read_json(os.path.join(sdir, "%02d.json" % n), {}) or {}
        for sh in sc.get("shots", []):
            sid = sh.get("id")
            if not sid:
                continue
            src = os.path.join(base, "clips", "%02d" % n, sid + ".mp4")
            dst = os.path.join(base, "clips4k", "%02d" % n, sid + ".mp4")
            if not os.path.isfile(src):
                continue
            if redo or not os.path.isfile(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
                out.append("%d:%s" % (n, sid))
    return out


def _all_shot_keys(slug, redo=False):
    """Every shot in chapter order; by default only those with no clip yet."""
    out = []
    base = os.path.join(PROJECTS, slug)
    for n in sorted(int(f[:2]) for f in os.listdir(os.path.join(base, "script"))
                    if re.match(r"^\d\d\.json$", f)) if os.path.isdir(os.path.join(base, "script")) else []:
        sc = _read_json(os.path.join(base, "script", "%02d.json" % n), {}) or {}
        for sh in sc.get("shots", []):
            sid = sh.get("id")
            if not sid:
                continue
            if redo or not os.path.isfile(os.path.join(base, "clips", "%02d" % n, sid + ".mp4")):
                out.append("%d:%s" % (n, sid))
    return out


def _srt_time(t):
    t = max(0.0, float(t))
    h, r = divmod(t, 3600)
    m, sec = divmod(r, 60)
    return "%02d:%02d:%02d,%03d" % (int(h), int(m), int(sec), round((sec - int(sec)) * 1000))


# ----------------------------------------------------------------------
# the chapter film -- the clips of one chapter, in script order, behind a
# title card
#
# A fourth queue was NOT built for this, and that is deliberate: joining is a
# stream copy plus a three-second card, seconds of CPU against the ~27 GPU-min
# a single segment costs, so it runs inline the moment a chapter's last clip
# lands and never competes for the GPU with the render, the 4K pass or a bed.

FILM_TITLE_SECONDS = float(os.environ.get("BOOKREEL_TITLE_SECONDS", "3.0"))
# A .ttc collection, so it needs an index; the SC face is first.
FILM_FONT = os.environ.get(
    "BOOKREEL_TITLE_FONT", "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc")
_FILM_LOCK = threading.Lock()


def film_dir(slug):
    return os.path.join(PROJECTS, slug, "film")


def chapter_film(slug, n):
    return os.path.join(film_dir(slug), "%02d.mp4" % int(n))


def _archive_film(out):
    """A film is never overwritten (2026-10-14: "生成的 VIDEO 也加生成版本控制，
    不要直接覆盖"). The one on disk moves to film/_versions/<stem>-<stamp>.mp4,
    stamped with ITS OWN time, together with its .srt; the new one then takes
    the plain name. Returns where the old one went, or ""."""
    if not os.path.isfile(out):
        return ""
    vdir = os.path.join(os.path.dirname(out), "_versions")
    os.makedirs(vdir, exist_ok=True)
    stem = os.path.basename(out)[:-4]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(os.path.getmtime(out)))
    dst = os.path.join(vdir, "%s-%s.mp4" % (stem, stamp))
    k = 1
    while os.path.exists(dst):
        k += 1
        dst = os.path.join(vdir, "%s-%s-%d.mp4" % (stem, stamp, k))
    os.replace(out, dst)
    srt = out[:-4] + ".srt"
    if os.path.isfile(srt):
        os.replace(srt, dst[:-4] + ".srt")
    return dst


def film_versions(slug, stem):
    """Older builds of film/<stem>.mp4, newest first: [{name, url, mb, seconds}]."""
    vdir = os.path.join(film_dir(slug), "_versions")
    out = []
    for p in sorted(glob.glob(os.path.join(vdir, glob.escape(stem) + "-*.mp4")), reverse=True):
        rel = "film/_versions/" + os.path.basename(p)
        out.append({"name": os.path.basename(p)[:-4],
                    "url": "/media/%s/%s" % (slug, urllib.parse.quote(rel)),
                    "mb": round(os.path.getsize(p) / 1e6, 1),
                    "seconds": round(_duration(p), 1)})
    return out


_CN_NUM = "〇一二三四五六七八九十"


def _cn_number(n):
    """1 -> 一, 11 -> 十一, 20 -> 二十. Chapters do not run past 99 here."""
    n = int(n)
    if n < 10:
        return _CN_NUM[n]
    if n < 20:
        return "十" + (_CN_NUM[n - 10] if n > 10 else "")
    return _CN_NUM[n // 10] + "十" + (_CN_NUM[n % 10] if n % 10 else "")


def chapter_title(slug, n):
    """(work, 第N章, chapter title) -- whatever of it exists."""
    work = str((_read_json(os.path.join(PROJECTS, slug, "blueprint.json"), {}) or {})
               .get("title") or "").strip()
    ch = _read_json(os.path.join(PROJECTS, slug, "chapters", "%02d.json" % int(n)), {}) or {}
    return work, "第%s章" % _cn_number(n), str(ch.get("title") or "").strip()


def chapter_clips(slug, n):
    """The chapter's clips in SCRIPT order, and how many shots it should have.

    Same walk as chapter_srt, so the picture and the subtitles cannot drift
    apart: a shot with no clip is skipped by both.
    """
    base = os.path.join(PROJECTS, slug)
    sc = _read_json(os.path.join(base, "script", "%02d.json" % int(n)), {}) or {}
    parts, total = [], 0
    for sh in sc.get("shots", []):
        sid = sh.get("id")
        if not sid:
            continue
        total += 1
        f = os.path.join(base, "clips", "%02d" % int(n), sid + ".mp4")
        if os.path.isfile(f):
            parts.append(f)
    return parts, total


# Music under the finished chapter. Not a drone: the script writes music as
# something that ENTERS, holds and LEAVES (sound.music.cue = 进 / 延续 / 出 /
# 无), and chapter 1 uses that six times across thirty shots. A bed that simply
# runs for the whole chapter is the stutter's opposite number - it stops being
# scoring and becomes wallpaper.
FILM_MUSIC = os.environ.get("BOOKREEL_FILM_MUSIC", "1") not in ("0", "", "false")
FILM_MUSIC_GAIN = float(os.environ.get("BOOKREEL_FILM_MUSIC_GAIN", "0.28"))
FILM_MUSIC_FADE = float(os.environ.get("BOOKREEL_FILM_MUSIC_FADE", "1.5"))


def music_spans(slug, n, offset=0.0, planned=False):
    """[(location_id, start, end)] in FILM time, read off the shots' own cues.

    A span opens on 进, holds through 延续, closes after 出, and 无 ends it.
    A 出 with no 进 before it is an orphan (chapter 1 has one, C01S18) and
    opens nothing. A location change closes the span too: there is one bed per
    location, and crossfading two of them under one run is not something the
    script ever asks for.

    `planned=True` costs the spans off the script's own `seconds` instead of
    off the finished clips, so the length a bed has to cover is known in step 2
    - before a single frame is rendered, which is when the beds are generated.
    """
    base = os.path.join(PROJECTS, slug)
    sc = _read_json(os.path.join(base, "script", "%02d.json" % int(n)), {}) or {}
    spans, cur, t = [], None, float(offset)
    for sh in sc.get("shots", []):
        sid = sh.get("id")
        if not sid:
            continue
        f = os.path.join(base, "clips", "%02d" % int(n), sid + ".mp4")
        if planned:
            dur = _duration(f) if os.path.isfile(f) else max(
                VIDEO_MIN_SECONDS, min(VIDEO_VOICE_MAX_SECONDS,
                                       float(sh.get("seconds") or 0) or VIDEO_SILENT_SECONDS))
        else:
            if not os.path.isfile(f):
                continue                  # not in the film, so not on its clock
            dur = _duration(f)
        cue = str((((sh.get("sound") or {}).get("music")) or {}).get("cue") or "").strip()
        lid = str(sh.get("location_id") or "").strip()
        if cur and (cue in ("进", "无", "") or lid != cur["lid"]):
            spans.append((cur["lid"], cur["start"], t))
            cur = None
        if cue == "进" and lid:
            cur = {"lid": lid, "start": t}
        if cur:
            if cue == "出":
                spans.append((cur["lid"], cur["start"], t + dur))
                cur = None
        t += dur
    if cur:
        spans.append((cur["lid"], cur["start"], t))
    return [(l, s, e) for l, s, e in spans if e - s > 1.0]


def _mix_music(slug, n, film, spans, dst):
    """Lay the location beds under a finished chapter film.

    Picture is stream-copied - only the audio is touched. The beds are ducked
    against the film's own audio (sidechaincompress) rather than simply set
    quiet: narration is the point of this soundtrack, and a fixed level that
    is safe under speech is inaudible under silence.
    """
    args, filt, labels = ["-i", film], [], []
    # Where each location's bed has been played up to. The next entrance at
    # that location starts THERE, not at zero: six entrances in chapter 1 used
    # to be six plays of the same opening minute, and a span longer than the
    # bed used to loop it outright. Both are the same fault - a bed you have
    # already heard is wallpaper - and this is what fixes them.
    used, reused = {}, []
    for i, (lid, start, end) in enumerate(spans):
        bed = os.path.join(music_dir(slug), lid + ".mp3")
        if not os.path.isfile(bed):
            continue
        dur = max(0.5, end - start)
        fade = min(FILM_MUSIC_FADE, dur / 3.0)
        have = _duration(bed)
        off = used.get(lid, 0.0)
        if have > 0 and off + dur > have - 0.05:
            # Not enough fresh music left. Go back to the top and say so by
            # name rather than looping silently - the cure is a longer bed
            # (music_seconds), and nobody can ask for one they cannot see.
            off = 0.0
            reused.append(lid)
        args += ["-ss", "%.3f" % off, "-i", bed]
        lab = "m%d" % i
        filt.append(
            "[%d:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            "atrim=0:%.3f,asetpts=N/SR/TB,"
            "afade=t=in:st=0:d=%.2f,afade=t=out:st=%.3f:d=%.2f,"
            "volume=%.3f,adelay=%d|%d[%s]"
            % (len(labels) + 1, dur, fade, max(0.0, dur - fade), fade,
               FILM_MUSIC_GAIN, int(start * 1000), int(start * 1000), lab))
        labels.append(lab)
        used[lid] = off + dur
    if reused:
        print("[web] ! %s 第 %s 章：%s 的底乐不够长，%d 处只能从头再放一次；"
              "把这几处的底乐重出一遍就会按新长度生成"
              % (slug, n, "、".join(sorted(set(reused))), len(reused)), flush=True)
    if not labels:
        return False
    filt.append("[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[a0][a1]")
    if len(labels) > 1:
        filt.append("%samix=inputs=%d:normalize=0[mus]"
                    % ("".join("[%s]" % l for l in labels), len(labels)))
    else:
        filt.append("[%s]anull[mus]" % labels[0])
    filt.append("[mus][a1]sidechaincompress=threshold=0.05:ratio=6:attack=15:release=350[duck]")
    filt.append("[a0][duck]amix=inputs=2:normalize=0:duration=first[aout]")
    _ff(args + ["-filter_complex", ";".join(filt),
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                dst], timeout=1800)
    return True


def _stream_params(path):
    """(w, h, fps, tbn) off ffmpeg's own report; there is no ffprobe here."""
    exe = _ffmpeg()
    if not exe or not os.path.isfile(path):
        return None
    pr = subprocess.run([exe, "-hide_banner", "-i", path], capture_output=True, timeout=120)
    txt = pr.stderr.decode("utf-8", "replace")
    m = re.search(r"Video: .*?, (\d+)x(\d+).*?, ([\d.]+) fps, [\d.]+ tbr, (\d+)k? tbn", txt)
    if not m:
        m2 = re.search(r"Video: .*?, (\d+)x(\d+).*?, ([\d.]+) fps", txt)
        if not m2:
            return None
        return int(m2.group(1)), int(m2.group(2)), float(m2.group(3)), 0
    return int(m.group(1)), int(m.group(2)), float(m.group(3)), int(m.group(4))


def _title_png(slug, n, w, h, dst):
    """The card, drawn with PIL rather than ffmpeg's drawtext.

    drawtext needs an ffmpeg built with freetype AND fontconfig, and the one
    imageio-ffmpeg ships is not guaranteed to have either; PIL is already a
    dependency and reads the .ttc directly, so the card cannot fail for a
    reason nobody can see.
    """
    from PIL import Image, ImageDraw, ImageFont

    work, ch_no, ch_title = chapter_title(slug, n)
    img = Image.new("RGB", (w, h), (8, 8, 10))
    d = ImageDraw.Draw(img)

    def font(px):
        try:
            return ImageFont.truetype(FILM_FONT, px, index=0)
        except OSError:
            return ImageFont.load_default()

    big = font(int(h * 0.11))
    mid = font(int(h * 0.055))
    small = font(int(h * 0.035))
    # Measure before drawing: a block laid out from nominal point sizes sits
    # visibly low, because a CJK glyph box is not its font size.
    gap_work, gap_no, rule = int(h * 0.06), int(h * 0.035), int(h * 0.05)
    lines = [(work, small, (150, 140, 125), gap_work),
             (ch_no, mid, (196, 180, 150), gap_no),
             (ch_title, big, (238, 232, 220), 0)]
    lines = [ln for ln in lines if ln[0]]
    total = sum((d.textbbox((0, 0), t, font=f)[3] - d.textbbox((0, 0), t, font=f)[1]) + g
                for t, f, _c, g in lines)
    y = (h - total) / 2
    for text, f, fill, gap in lines:
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        d.text(((w - (r - l)) / 2 - l, y - t), text, font=f, fill=fill)
        y += (b - t) + gap
    # a hairline under the title
    d.line([(w * 0.38, y + rule), (w * 0.62, y + rule)], fill=(120, 108, 92), width=2)
    img.save(dst)


def _title_clip(slug, n, like, dst):
    """A title card encoded to match `like` exactly, so concat can stream-copy.

    Matching matters: the join is `-c copy`, and a card that differs in
    resolution, pixel format, frame rate, timescale or audio layout either
    fails the concat outright or produces a file whose clock is wrong from the
    card onwards.
    """
    p = _stream_params(like) or (1312, 736, 24.0, 12288)
    w, h, fps, tbn = p
    png = dst + ".png"
    _title_png(slug, n, w, h, png)
    part = dst[:-4] + ".part.mp4"
    try:
        args = ["-loop", "1", "-framerate", "%g" % fps, "-t", "%.3f" % FILM_TITLE_SECONDS, "-i", png,
                "-f", "lavfi", "-t", "%.3f" % FILM_TITLE_SECONDS,
                "-i", "anullsrc=r=48000:cl=stereo",
                "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-crf", "18", "-r", "%g" % fps,
                "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", "-shortest"]
        if tbn:
            args += ["-video_track_timescale", str(tbn)]
        _ff(args + [part], timeout=300)
        os.replace(part, dst)
    finally:
        for f in (png, part):
            try:
                os.unlink(f)
            except OSError:
                pass
    return dst


# The script's soft transitions, in the film. `transitions.in` has said 淡入淡出
# / 叠化 since the first 分镜 and the join was a stream copy of hard cuts, so
# every scene change in every chapter was a jump (2026-09-28, "镜头缺乏过渡").
# A real cross-dissolve overlaps two clips and shortens the film, which moves
# every subtitle after it and the music spans with them; a DIP - the outgoing
# clip fades to black, the incoming one up from it - keeps each clip exactly
# its own length, so nothing downstream moves. Only the clips on either side of
# a soft cut are re-encoded (to the clip's own parameters, so the concat still
# stream-copies), and they are cached beside the film, rebuilt when the clip
# is newer.
FILM_DIP_SECONDS = float(os.environ.get("BOOKREEL_FILM_DIP_SECONDS", "0.5"))


def _film_dips(slug, n, parts):
    """parts with the clips around each soft transition swapped for faded copies."""
    if FILM_DIP_SECONDS <= 0 or not parts:
        return parts
    sc = _read_json(os.path.join(PROJECTS, slug, "script", "%02d.json" % int(n)), {}) or {}
    by_path = {}
    for sh in sc.get("shots", []):
        sid = sh.get("id")
        if sid:
            by_path[os.path.join(PROJECTS, slug, "clips", "%02d" % int(n), sid + ".mp4")] = sh
    fin, fout = set(), set()
    for i, p in enumerate(parts):
        sh = by_path.get(p) or {}
        tr = sh.get("transitions") or {}
        if any(w in str(tr.get("in") or "") for w in _SOFT_CUT):
            fin.add(i)
            if i > 0:
                fout.add(i - 1)
        if any(w in str(tr.get("out") or "") for w in _SOFT_CUT):
            fout.add(i)
            if i + 1 < len(parts):
                fin.add(i + 1)
    if not fin and not fout:
        return parts
    fx = os.path.join(film_dir(slug), "_dips", "%02d" % int(n))
    os.makedirs(fx, exist_ok=True)
    out = list(parts)
    d = FILM_DIP_SECONDS
    for i in sorted(fin | fout):
        src = parts[i]
        dst = os.path.join(fx, "%s-%d%d.mp4" % (os.path.basename(src)[:-4], i in fin, i in fout))
        if os.path.isfile(dst) and os.path.getmtime(dst) >= os.path.getmtime(src):
            out[i] = dst
            continue
        try:
            dur = _duration(src)
            w, h, fps, tbn = _stream_params(src) or (1312, 736, 24.0, 12288)
            vf, af = [], []
            if i in fin:
                vf.append("fade=t=in:st=0:d=%.3f" % d)
                af.append("afade=t=in:st=0:d=%.3f" % d)
            if i in fout:
                vf.append("fade=t=out:st=%.3f:d=%.3f" % (max(0.0, dur - d), d))
                af.append("afade=t=out:st=%.3f:d=%.3f" % (max(0.0, dur - d), d))
            part = dst[:-4] + ".part.mp4"
            args = ["-i", src, "-vf", ",".join(vf), "-af", ",".join(af),
                    "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
                    "-crf", "18", "-r", "%g" % fps,
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k"]
            if tbn:
                args += ["-video_track_timescale", str(tbn)]
            _ff(args + [part], timeout=600)
            os.replace(part, dst)
            out[i] = dst
        except Exception as exc:                        # noqa: BLE001
            # a missing fade is a hard cut, which is what the film had anyway
            print("[web] 第 %d 章过渡未做：%s %s" % (int(n), os.path.basename(src), str(exc)[:160]), flush=True)
    return out


def build_chapter_film(slug, n, force=False):
    """Join one chapter into a single mp4, title card first.

    Idempotent the way the 4K pass is: a film at least as new as every clip it
    was made from is that chapter's film and is left alone. Re-take a shot and
    its clip becomes newer, so the film is rebuilt.
    """
    n = int(n)
    parts, total = chapter_clips(slug, n)
    if not parts:
        raise RuntimeError("第 %d 章还没有片段" % n)
    out = chapter_film(slug, n)
    os.makedirs(film_dir(slug), exist_ok=True)
    newest = max(os.path.getmtime(p) for p in parts)
    # A re-generated bed has to rebuild the film too, or the chapter keeps
    # playing music that no longer exists on disk.
    for lid, _s, _e in (music_spans(slug, n) if FILM_MUSIC else []):
        bed = os.path.join(music_dir(slug), lid + ".mp3")
        if os.path.isfile(bed):
            newest = max(newest, os.path.getmtime(bed))
    if (not force and os.path.isfile(out) and os.path.getmtime(out) >= newest):
        return {"built": False, "path": out, "seconds": _duration(out),
                "clips": len(parts), "shots": total, "complete": len(parts) == total}

    with _FILM_LOCK:
        card = os.path.join(film_dir(slug), "_title-%02d.mp4" % n)
        _title_clip(slug, n, parts[0], card)
        parts = _film_dips(slug, n, parts)
        want = _duration(card) + sum(_duration(p) for p in parts)
        part = out[:-4] + ".part.mp4"     # .part.mp4, never .mp4.part
        _concat_stream([card] + parts, part)
        got = _duration(part)
        if abs(got - want) > 1.0:
            # Stream copy produced a file whose clock does not add up - mixed
            # timebases. Pay for one re-encode rather than publish a film that
            # drifts away from its own subtitles.
            lst = part + ".concat.txt"
            with open(lst, "w", encoding="utf-8") as f:
                for q in [card] + parts:
                    f.write("file '%s'\n" % os.path.abspath(q).replace("'", "'\\''"))
            try:
                _ff(["-f", "concat", "-safe", "0", "-i", lst,
                     "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-crf", "18",
                     "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k", part], timeout=3600)
            finally:
                try:
                    os.unlink(lst)
                except OSError:
                    pass
            got = _duration(part)

        # Music last, on the joined picture: one audio encode for the whole
        # chapter instead of one per clip, and the spans are in film time.
        spans = music_spans(slug, n, offset=_duration(card)) if FILM_MUSIC else []
        if spans:
            mixed = out[:-4] + ".mus.mp4"
            try:
                if _mix_music(slug, n, part, spans, mixed):
                    os.replace(mixed, part)
            except Exception as exc:                        # noqa: BLE001
                # A chapter with no score still cuts together; one that fails
                # to join does not. Keep the picture, lose the music, say so.
                print("[web] 第 %d 章配乐未混入：%s" % (n, str(exc)[:200]), flush=True)
                try:
                    os.unlink(mixed)
                except OSError:
                    pass
        old = _archive_film(out)
        if old:
            print("[web] 第 %d 章上一版成片存为 %s" % (n, os.path.relpath(old, ROOT)), flush=True)
        os.replace(part, out)

        # Subtitles beside the film, shifted by the card: the cues describe the
        # clips, and the clips now start FILM_TITLE_SECONDS in.
        body, info = chapter_srt(slug, n, offset=_duration(card))
        with open(os.path.join(film_dir(slug), "%02d.srt" % n), "w", encoding="utf-8") as f:
            f.write(body)
    return {"built": True, "path": out, "seconds": round(got, 2), "clips": len(parts),
            "shots": total, "complete": len(parts) == total, "cues": info.get("cues", 0),
            "music": len(spans)}


def chapter_rows(slug):
    """One row per chapter script, with the state of its film."""
    base = os.path.join(PROJECTS, slug)
    sdir = os.path.join(base, "script")
    rows = []
    if not os.path.isdir(sdir):
        return rows
    for f in sorted(os.listdir(sdir)):
        if not re.match(r"^\d\d\.json$", f):
            continue
        n = int(f[:2])
        parts, total = chapter_clips(slug, n)
        film = chapter_film(slug, n)
        ex = os.path.isfile(film)
        stale = bool(ex and parts and os.path.getmtime(film) < max(os.path.getmtime(p) for p in parts))
        work, ch_no, ch_title = chapter_title(slug, n)
        rows.append({
            "n": n, "no": ch_no, "title": ch_title, "work": work,
            "shots": total, "clips": len(parts), "complete": bool(total and len(parts) == total),
            "exists": ex, "stale": stale,
            "seconds": round(_duration(film), 1) if ex else 0,
            "mb": round(os.path.getsize(film) / 1e6, 1) if ex else 0,
            "url": ("/media/%s/film/%02d.mp4?t=%d" % (slug, n, int(os.path.getmtime(film)))) if ex else "",
            "versions": film_versions(slug, "%02d" % n),
            "vtt": ("/api/vtt?slug=%s&n=%d&t=%d" % (slug, n, int(os.path.getmtime(film)))) if ex else "",
        })
    return rows


def chapter_srt(slug, n, offset=0.0):
    """The chapter's subtitles, built from the clips that actually exist.

    Timing comes from two things on disk and nothing else: how long each clip
    runs, and where each line sits inside the voice track that was muxed into
    it. Both are recorded when the clip is rendered, so the subtitles describe
    the audio in the film rather than a second synthesis of the same words.
    Shots with no clip yet are skipped and do not advance the clock - they are
    not in the concatenated chapter either.
    """
    base = os.path.join(PROJECTS, slug)
    sc = _read_json(os.path.join(base, "script", "%02d.json" % int(n)), {}) or {}
    # `offset` starts where the picture starts: 0 for the bare clip sequence,
    # the title card's length for the film built on top of it.
    out, idx, offset, missing = [], 1, float(offset), 0
    for sh in sc.get("shots", []):
        sid = sh.get("id")
        if not sid:
            continue
        full = os.path.join(base, "clips", "%02d" % int(n), sid + ".mp4")
        if not os.path.isfile(full):
            continue
        d, _ext = _take_dir(full)
        take = _take_active(full)
        meta = _read_json(os.path.join(d, "%d.json" % take), {}) if take else {}
        meta = meta or {}
        clip = 0.0
        try:
            clip = float(meta.get("seconds") or 0.0)
        except (TypeError, ValueError):
            clip = 0.0
        if not clip:
            clip = _duration(full)
        cues = meta.get("cues")
        if cues is None:
            missing += 1
            cues = []
        for c in _clamp_cues(cues, clip):
            try:
                st, en = float(c["start"]) + offset, float(c["end"]) + offset
            except (KeyError, TypeError, ValueError):
                continue
            text = (c.get("text") or "").strip()
            if not text or en <= st:
                continue
            out.append("%d\n%s --> %s\n%s\n" % (idx, _srt_time(st), _srt_time(en), text))
            idx += 1
        offset += clip
    return "\n".join(out), {"cues": idx - 1, "seconds": round(offset, 2), "stale": missing}


def video_plan(slug):
    """What each shot would use as its first frame, without rendering anything."""
    base = os.path.join(PROJECTS, slug)
    lib = _read_json(os.path.join(base, "assets", "prompts.json"), {}) or {}
    rows = []
    if not os.path.isdir(os.path.join(base, "script")):
        return {"shots": rows}
    for n in sorted(int(f[:2]) for f in os.listdir(os.path.join(base, "script")) if re.match(r"^\d\d\.json$", f)):
        sc = _read_json(os.path.join(base, "script", "%02d.json" % n), {}) or {}
        for sh in sc.get("shots", []):
            ref, note, _plate = _pick_first_frame(slug, sh, lib)
            rel = "clips/%02d/%s.mp4" % (n, sh.get("id", ""))
            rows.append({"key": "%d:%s" % (n, sh.get("id")), "n": n, "id": sh.get("id"),
                         "seconds": sh.get("seconds"), "slug_line": sh.get("slug_line", ""),
                         "first_frame": ref, "first_frame_note": note,
                         "script_ref": sh.get("first_frame_ref", ""),
                         "done": os.path.isfile(os.path.join(base, *rel.split("/"))),
                         "url": ("/media/%s/%s" % (slug, rel)) if os.path.isfile(os.path.join(base, *rel.split("/"))) else None})
    return {"shots": rows, "total": len(rows), "done": sum(1 for r in rows if r["done"]),
            "seconds": sum(r["seconds"] or 10 for r in rows)}


def video_batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _VIDEO:
            _VIDEO[slug] = VideoBatch(slug)
        return _VIDEO[slug]


_BATCHES = {}
_BATCHES_LOCK = threading.Lock()


def batch_for(slug):
    with _BATCHES_LOCK:
        if slug not in _BATCHES:
            _BATCHES[slug] = Batch(slug)
        return _BATCHES[slug]


# ----------------------------------------------------------------------
# 一键：小说 -> 成片 (AutoRun, /api/autorun, the hub's 载入小说 form)
# ----------------------------------------------------------------------
# Every stage below already exists and has its own page; what did not exist
# was somebody to press them in order. Loading a novel used to mean running
# scripts/import-novel.py by hand, then Story Studio (关系图, 分镜 per chapter,
# 资产提示词), stopping the LLM, starting ComfyUI, Reference Images (出图),
# Pipeline Runner (出片) - with a GPU hand-over in the middle that CLAUDE.md
# says must not be forgotten. This is that sequence, in the server, for the
# same reason the render queues live here: a browser-driven run dies with the
# tab, and this one runs for days.
#
# Each stage asks the DISK whether it is done (a blueprint, relations.json,
# script/NN.json, prompts.json, stage.json, the images, the clips), so a
# restart resumes where the files say it got to, and a stage the person did
# by hand is not redone. Stop takes effect between stages; an LLM call in
# flight (an import is hours) finishes first, because story_server cannot be
# interrupted mid-generation.
STORY = os.environ.get("BOOKREEL_STORY_URL", "http://127.0.0.1:8010").rstrip("/")
AUTORUN_POLL = 30
AUTORUN_STAGES = [
    ("wait",      "等 GPU 空出来"),
    ("import",    "导入原文：分章、分段、摘要、蓝图"),
    ("relations", "人物关系与派系"),
    ("script",    "逐段分镜"),
    ("assets",    "资产提示词"),
    ("stage",     "场景平面（预演）"),
    ("handover",  "卸载大模型，起 ComfyUI"),
    ("images",    "出参考图"),
    ("recheck",   "复核未判定的参考图"),
    ("sound",     "音效库与场景配乐"),
    ("video",     "按章出片"),
    ("books",     "按原书章节合成"),
]


def _novel_slug(title):
    """story_server._slug's rule, so both servers name the project the same."""
    s = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    s = re.sub(r"[^\w一-鿿-]+", "-", s).strip("-")[:60] or "novel"
    base, k = s, 2
    while os.path.exists(os.path.join(PROJECTS, s)):
        s = "%s-%d" % (base[:56], k)
        k += 1
    return s


def _decode_novel(raw):
    for enc in ("utf-8-sig", "gb18030", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _story_post(path, body, timeout):
    req = urllib.request.Request(STORY + path, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read() or b"{}").get("detail")
        except ValueError:
            detail = None
        raise RuntimeError("故事引擎 %s 返回 %d：%s" % (path, exc.code, detail or exc.reason))
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError("连不上故事引擎（%s）：%s —— 先 ./launch.sh restart-one story" % (STORY, exc))


def _comfy_alive():
    try:
        urllib.request.urlopen(COMFY + "/queue", timeout=5).read()
        return True
    except Exception:                                       # noqa: BLE001
        return False


def _gpu_queues_busy(exclude=None):
    """Which render queues, in ANY project, are holding the GPU right now."""
    busy = []
    for name, reg in (("图片", _BATCHES), ("片段", _VIDEO), ("口型同步", _LIPSYNC),
                      ("4K", _UPSCALE), ("配乐", _MUSIC), ("音效", _SFX)):
        for sg, b in list(reg.items()):
            if sg == exclude:
                continue
            if (b.state or {}).get("status") == "running" and b.thread and b.thread.is_alive():
                busy.append("%s/%s" % (sg, name))
    return busy


def book_groups(slug):
    """[(book_n, title, [part n...])] from the blueprint an import wrote."""
    bp = _read_json(os.path.join(PROJECTS, slug, "blueprint.json"), {}) or {}
    out = []
    for c in bp.get("chapters") or []:
        k = c.get("book_n")
        if not k:
            continue
        if not out or out[-1][0] != k:
            out.append((k, c.get("book_title") or "", []))
        out[-1][2].append(int(c.get("n")))
    return out


def book_film(slug, k):
    return os.path.join(film_dir(slug), "book-%02d.mp4" % int(k))


def _srt_cues(path):
    try:
        body = open(path, encoding="utf-8").read()
    except OSError:
        return []
    ts = r"(\d+):(\d+):(\d+),(\d+)"
    cues = []
    for blk in re.split(r"\n\s*\n", body.strip()):
        lines = blk.strip().splitlines()
        for i, ln in enumerate(lines):
            m = re.match(ts + r"\s*-->\s*" + ts, ln.strip())
            if m:
                g = [int(x) for x in m.groups()]
                a = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
                b = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
                cues.append((a, b, "\n".join(lines[i + 1:])))
                break
    return cues


def build_book_films(slug, only_part=None):
    """Join the part films of each ORIGINAL book chapter into film/book-NN.mp4.

    An imported novel is split into ~1500-character parts because that is what
    one 分镜 reply holds, and each part gets its own film. A reader knows the
    book by its own chapters, so once every part of one has its film, the
    films are stream-copied end to end (each part keeps its title card, which
    reads as a section card) and the subtitles are shifted onto one clock.
    Idempotent like the part films: skipped when newer than every input."""
    built = []
    for k, title, ns in book_groups(slug):
        if only_part is not None and int(only_part) not in ns:
            continue
        films = [chapter_film(slug, n) for n in ns]
        if not all(os.path.isfile(f) for f in films):
            continue
        if any(len(p) != t for p, t in (chapter_clips(slug, n) for n in ns)):
            continue
        out = book_film(slug, k)
        if os.path.isfile(out) and os.path.getmtime(out) >= max(os.path.getmtime(f) for f in films):
            continue
        part = out[:-4] + ".part.mp4"
        with _FILM_LOCK:
            _concat_stream(films, part)
            _archive_film(out)
            os.replace(part, out)
            lines, clock, idx = [], 0.0, 1
            for n, f in zip(ns, films):
                for a, b, text in _srt_cues(os.path.join(film_dir(slug), "%02d.srt" % n)):
                    lines.append("%d\n%s --> %s\n%s\n" % (idx, _srt_time(a + clock), _srt_time(b + clock), text))
                    idx += 1
                clock += _duration(f)
            with open(out[:-4] + ".srt", "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
        built.append({"book": k, "title": title, "parts": len(ns), "seconds": round(_duration(out), 1)})
    return built


class AutoRun:
    def __init__(self, slug):
        self.slug = slug
        self.path = os.path.join(PROJECTS, slug, "autorun.json")
        self.lock = threading.Lock()
        self.thread = None
        self.stop_flag = False
        self.state = _read_json(self.path, None) or {"status": "idle", "stage": "", "log": []}
        self._waiting_on = None

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def _log(self, msg):
        print("[autorun] %s: %s" % (self.slug, msg), flush=True)
        with self.lock:
            self.state.setdefault("log", []).append({"t": int(time.time()), "msg": msg})
            self.state["log"] = self.state["log"][-120:]
            self._save()

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)
            self._save()

    def snapshot(self):
        with self.lock:
            st = dict(self.state)
        st["slug"] = self.slug
        st["running"] = bool(self.thread and self.thread.is_alive())
        st["stop_requested"] = self.stop_flag
        st["stages"] = [{"key": k, "label": l} for k, l in AUTORUN_STAGES]
        st["films"] = [{"book": k, "title": t,
                        "url": "/media/%s/film/book-%02d.mp4?t=%d" % (
                            urllib.parse.quote(self.slug), k, int(os.path.getmtime(book_film(self.slug, k))))}
                       for k, t, _ns in book_groups(self.slug) if os.path.isfile(book_film(self.slug, k))]
        return st

    def start(self, opts=None):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return {"ok": True, "already": True}
            if opts:
                self.state["opts"] = opts
            self.state.update({"status": "running", "error": "", "started": int(time.time()),
                               "finished": None})
            self.stop_flag = False
            self._save()
            self.thread = threading.Thread(target=self._run, name="autorun-" + self.slug, daemon=True)
            self.thread.start()
        return {"ok": True}

    def resume(self):
        if (self.state or {}).get("status") == "running":
            self._log("服务重启，续跑")
            self.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        q = self._waiting_on
        if q is not None:
            try:
                q.stop()
            except Exception:                               # noqa: BLE001
                pass
        self._log("收到停止：当前这一步结束后停下（正在进行的大模型调用会先跑完）")

    # -- helpers -------------------------------------------------------
    def _base(self, *rest):
        return os.path.join(PROJECTS, self.slug, *rest)

    def _stopped(self):
        if self.stop_flag:
            raise _AutoStop()

    def _wait(self, q, what):
        """Block until queue `q` has drained, logging progress now and then."""
        self._waiting_on = q
        last = 0
        try:
            while True:
                self._stopped()
                st = q.snapshot()
                if not st.get("running"):
                    return st
                if time.time() - last > 600:
                    last = time.time()
                    self._log("%s：已完成 %d，剩 %d%s" % (
                        what, len(st.get("done") or []), len(st.get("queue") or []),
                        ("，正在 " + str(st.get("current"))) if st.get("current") else ""))
                time.sleep(AUTORUN_POLL)
        finally:
            self._waiting_on = None

    def _story(self, path, body, timeout, done=None):
        """One story_server call, but never beside another generation. A
        web_server restart re-enters the stage whose call is STILL running in
        story_server (it cannot be interrupted); calling again would run the
        same import or 分镜 twice over on one GPU. Wait for it, then ask the
        disk - the stage functions re-check it - before calling."""
        said = False
        while True:
            self._stopped()
            try:
                with urllib.request.urlopen(STORY + "/progress", timeout=10) as r:
                    active = bool(json.loads(r.read() or b"{}").get("active"))
            except Exception:                               # noqa: BLE001
                active = False
            if not active:
                break
            if not said:
                self._log("故事引擎正忙（上一次的生成还在跑），等它结束")
                said = True
            time.sleep(AUTORUN_POLL)
        if done and os.path.isfile(done):
            return None
        return _story_post(path, body, timeout)

    def _chapters(self):
        bp = _read_json(self._base("blueprint.json"), {}) or {}
        return [int(c.get("n")) for c in bp.get("chapters") or [] if c.get("n")]

    # -- the run -------------------------------------------------------
    def _run(self):
        try:
            for key, label in AUTORUN_STAGES:
                self._stopped()
                self._set(stage=key, stage_label=label)
                getattr(self, "_s_" + key)()
            left = _all_shot_keys(self.slug)
            if left:
                self._set(status="incomplete", stage="", stage_label="", finished=int(time.time()))
                self._log("流程走完，但还有 %d 镜没出成；点「继续」只重出这些" % len(left))
            else:
                self._set(status="done", stage="", stage_label="", finished=int(time.time()))
                self._log("全部完成：成片在 projects/%s/film/（book-NN.mp4 为原书各章）" % self.slug)
        except _AutoStop:
            self._set(status="stopped", finished=int(time.time()))
            self._log("已停止（再点「继续」从这一步接着跑）")
        except Exception as exc:                            # noqa: BLE001
            self._set(status="failed", error=str(exc)[:500], finished=int(time.time()))
            self._log("× %s 失败：%s" % (self.state.get("stage_label") or "", str(exc)[:300]))

    def _s_wait(self):
        # Text generation wants ComfyUI stopped (it holds 21-25 GB of a 31 GB
        # host), and stopping it under somebody else's render loses that
        # render. So wait for the machine rather than take it.
        if all(os.path.isfile(p) for p in (self._base("blueprint.json"), self._base("relations.json"),
                                           self._base("assets", "prompts.json"))) \
                and all(os.path.isfile(self._base("script", "%02d.json" % n)) for n in self._chapters()):
            return
        said = False
        while True:
            self._stopped()
            busy = _gpu_queues_busy(exclude=self.slug)
            if not busy:
                break
            if not said:
                self._log("等这些队列空下来再停 ComfyUI 写剧本：%s" % "、".join(busy))
                said = True
            time.sleep(AUTORUN_POLL)
        if _comfy_alive():
            self._log("停 ComfyUI，把主机内存让给写剧本的大模型")
            subprocess.run([os.path.join(ROOT, "launch.sh"), "comfy-stop"], capture_output=True, timeout=600)

    def _s_import(self):
        if os.path.isfile(self._base("blueprint.json")):
            return
        src = self._base("source", "original.txt")
        try:
            text = open(src, encoding="utf-8").read()
        except OSError:
            raise RuntimeError("没有原文（%s）" % src)
        o = self.state.get("opts") or {}
        dry = self._story("/import", {"slug": self.slug, "text": text, "dry_run": True,
                                      "part_chars": o.get("part_chars") or 1500}, 300)
        books = dry.get("books") or []
        self._log("分章：原书 %d 章 → %d 段（每段一次分镜、一部分段成片），共 %d 字"
                  % (len(books), dry.get("parts", 0), dry.get("chars", 0)))
        self._set(books=books, parts=dry.get("parts", 0))
        self._log("摘要每段约 2 分钟，蓝图另需 10-20 分钟")
        r = self._story("/import", {"slug": self.slug, "text": text, "title": o.get("title") or "",
                                    "source": o.get("source") or "", "era": o.get("era") or "",
                                    "style": o.get("style") or "", "notes": o.get("notes") or "",
                                    "part_chars": o.get("part_chars") or 1500}, 24 * 3600,
                        done=self._base("blueprint.json"))
        if r is None:
            return
        self._log("导入完成：%d 段，%d 位人物，%d 处场景，时代 %s"
                  % (r.get("parts", 0), r.get("characters", 0), r.get("locations", 0), r.get("era") or "—"))

    def _s_relations(self):
        if os.path.isfile(self._base("relations.json")):
            return
        r = self._story("/relations", {"slug": self.slug}, 3 * 3600, done=self._base("relations.json"))
        if r is None:
            return
        self._log("关系图：%d 个派系，%d 条关系" % (len(r.get("factions") or []), len(r.get("edges") or [])))

    def _s_script(self):
        ns = self._chapters()
        todo = [n for n in ns if not os.path.isfile(self._base("script", "%02d.json" % n))]
        for i, n in enumerate(todo, 1):
            self._stopped()
            self._set(stage_label="逐段分镜（%d / %d）" % (len(ns) - len(todo) + i, len(ns)))
            r = self._story("/script", {"slug": self.slug, "n": n}, 3 * 3600,
                            done=self._base("script", "%02d.json" % n))
            if r is None:
                continue
            self._log("第 %d 段分镜：%d 镜" % (n, len(r.get("shots") or [])))

    def _s_assets(self):
        if os.path.isfile(self._base("assets", "prompts.json")):
            return
        r = self._story("/assets", {"slug": self.slug}, 4 * 3600, done=self._base("assets", "prompts.json"))
        if r is None:
            return
        self._log("资产提示词：%d 条" % len(r.get("items") or []))

    def _s_stage(self):
        if os.path.isfile(self._base("assets", "stage.json")):
            return
        # Previz is an improvement, not a requirement: a shot with no staging
        # renders exactly as it did before previz existed.
        try:
            self._story("/stage", {"slug": self.slug}, 2 * 3600, done=self._base("assets", "stage.json"))
            self._log("场景平面完成")
        except Exception as exc:                            # noqa: BLE001
            self._log("！场景平面没做成（不影响出片，只是没有预演控制）：%s" % str(exc)[:160])

    def _s_handover(self):
        try:
            j = self._story("/unload", {}, 300)
            if j.get("freed_gb"):
                self._log("大模型已卸载，释放 %s GB" % j.get("freed_gb"))
        except Exception as exc:                            # noqa: BLE001
            self._log("！卸载大模型失败（继续）：%s" % str(exc)[:120])
        if not _comfy_alive():
            self._log("起 ComfyUI")
            subprocess.run([os.path.join(ROOT, "launch.sh"), "comfy-restart"], capture_output=True, timeout=1200)
        # The queues retry while ComfyUI is away, so a slow start costs only time.

    def _items(self):
        return (_read_json(self._base("assets", "prompts.json"), {}) or {}).get("items") or []

    def _s_images(self):
        keys = [i["key"] for i in self._items()
                if i.get("key") and i.get("file")
                and not os.path.isfile(self._base("assets", "images", i["file"]))]
        if not keys:
            return
        self._log("出参考图：%d 张（每张约 7 分钟，含核对）" % len(keys))
        b = batch_for(self.slug)
        b.start(keys, "first")
        st = self._wait(b, "参考图")
        if st.get("failed"):
            self._log("！%d 张参考图没出成：%s" % (len(st["failed"]), "、".join(st["failed"][:8])))

    def _s_recheck(self):
        # CLAUDE.md: a verdict of null is not a pass. A plate with an unasked
        # head count becomes the first frame of every cold shot there.
        keys = []
        for i in self._items():
            full = self._base("assets", "images", i.get("file") or "")
            if not i.get("key") or not os.path.isfile(full):
                continue
            n = _take_active(full)
            meta = _read_json(os.path.join(_take_dir(full)[0], "%d.json" % n), {}) if n else {}
            if ((meta or {}).get("check") or {}).get("ok") is None:
                keys.append(i["key"])
        if not keys:
            return
        self._log("复核 %d 张没有判定的参考图" % len(keys))
        b = batch_for(self.slug)
        b.start(keys, "check")
        self._wait(b, "复核")

    def _s_sound(self):
        try:
            ids = [r["id"] for r in sfx_items(self.slug) if not r["exists"] and not r["hand"]]
            if ids:
                sfx_batch_for(self.slug).start(ids, False)
                self._wait(sfx_batch_for(self.slug), "音效")
            ids = [r["id"] for r in music_items(self.slug) if not r["exists"]]
            if ids:
                music_batch_for(self.slug).start(ids, False)
                self._wait(music_batch_for(self.slug), "配乐")
        except _AutoStop:
            raise
        except Exception as exc:                            # noqa: BLE001
            # A film with no score still cuts together.
            self._log("！音效/配乐没做成（不影响出片）：%s" % str(exc)[:160])

    def _s_video(self):
        keys = _all_shot_keys(self.slug)
        if not keys:
            return
        self._log("出片：%d 镜，按段顺序；每段出齐自动合成该段成片" % len(keys))
        vb = video_batch_for(self.slug)
        vb.start(keys)
        st = self._wait(vb, "出片")
        if st.get("failed"):
            self._log("！%d 镜没出成，可在「成片」页单独重出：%s"
                      % (len(st["failed"]), "、".join(str(x) for x in st["failed"][:8])))

    def _s_books(self):
        for n in self._chapters():
            parts, total = chapter_clips(self.slug, n)
            if total and len(parts) == total:
                try:
                    build_chapter_film(self.slug, n)
                except Exception as exc:                    # noqa: BLE001
                    self._log("× 第 %d 段合成失败：%s" % (n, str(exc)[:160]))
        for b in build_book_films(self.slug):
            self._log("🎬 原书第 %d 章《%s》：%d 段，%.0f 秒" % (b["book"], b["title"], b["parts"], b["seconds"]))
        missing = [k for k, _t, _ns in book_groups(self.slug) if not os.path.isfile(book_film(self.slug, k))]
        if missing:
            self._log("！原书第 %s 章还有镜头没出齐，没有合成" % "、".join(str(k) for k in missing))


class _AutoStop(Exception):
    pass


_AUTORUN = {}


def autorun_for(slug):
    with _BATCHES_LOCK:
        if slug not in _AUTORUN:
            _AUTORUN[slug] = AutoRun(slug)
        return _AUTORUN[slug]


def autorun_list():
    out = []
    for slug in _project_slugs():
        p = os.path.join(PROJECTS, slug, "autorun.json")
        if slug in _AUTORUN or os.path.isfile(p):
            s = autorun_for(slug).snapshot()
            s["log"] = (s.get("log") or [])[-6:]
            out.append(s)
    return out


def _safe_slug(s):
    return re.sub(r"[^\w.-]", "", str(s or ""))[:80]


def _allowed(rel):
    """rel is a normalised, root-relative posix path with no leading slash."""
    if not rel or rel.startswith(".."):
        return False
    if rel in ALLOW_FILES:
        return True
    if rel.endswith(ALLOW_SUFFIX):
        # only at the top level -- no walking into subdirectories for pages
        return "/" not in rel
    return rel.startswith(ALLOW_DIRS)


def _pages():
    try:
        names = sorted(n for n in os.listdir(ROOT) if n.endswith(".dc.html"))
    except OSError:
        names = []
    # The runner is the app; the studio file is a design mockup. Runner first.
    names.sort(key=lambda n: (0 if "Runner" in n else 1, n))
    return names


class Handler(SimpleHTTPRequestHandler):
    # Quieter than the default, and without the reverse DNS lookup that makes
    # every request on a LAN take a second or two.
    def address_string(self):
        return self.client_address[0]

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] %s %s\n" % (self.address_string(), fmt % args))

    def do_HEAD(self):
        """Same routing, same allow-list, no body.

        Without this, HEAD fell through to SimpleHTTPRequestHandler, which
        resolves any path under ROOT and answers 200 -- so `GET /README.md` was
        refused while `HEAD /README.md` confirmed the file, its size and its
        mtime. The whole point of ALLOW is that this directory also holds
        models/, logs/, venvs/ and whatever token was pasted into it, and the
        server is reachable from the LAN whenever --host is 0.0.0.0.
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _body(self, data):
        """Write a response body unless this is a HEAD."""
        if not getattr(self, "_head_only", False):
            self.wfile.write(data)

    # Where a generated file may land inside a project. Anything else is
    # refused: this is the one route that writes, and it takes both a path and
    # a URL from the browser.
    # \w, not [A-Za-z0-9]: location plates are named by time of day
    # (loca_lo01_日.png) and the ASCII-only rule was rejecting every one of
    # them as "path 不合法" -- which the page reported as 落盘失败 after a
    # seven-minute render. No slashes, no dots-only names, that is all.
    SAVE_PATH = re.compile(r"^(assets/images|clips/[0-9]{2})/(?!\.+$)[\w.\-]{1,120}$")

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        if path == "/api/upload":
            return self._upload()
        if path == "/api/take":
            return self._take()
        if path == "/api/take/delete":
            return self._take(delete=True)
        if path in ("/api/batch/start", "/api/batch/stop"):
            return self._batch(path.endswith("stop"))
        if path in ("/api/autorun/load", "/api/autorun/preview"):
            return self._autorun_load(preview=path.endswith("preview"))
        if path in ("/api/autorun/start", "/api/autorun/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug or not os.path.isdir(os.path.join(PROJECTS, slug)):
                return self._json({"ok": False, "error": "没有这个项目"}, code=404)
            ar = autorun_for(slug)
            if path.endswith("stop"):
                ar.stop()
            else:
                ar.start()
            return self._json({"ok": True, "run": ar.snapshot()})
        if path == "/api/lipcheck":
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            n = int(req.get("n") or 1)
            # qwen3-vl runs on the GPU, and so does the sampler. One or two
            # calls beside a render is an hour on the critical path - the same
            # arithmetic that keeps the music beds out of a render. So this is
            # a between-chapters pass, and it says so rather than quietly
            # competing.
            b = video_batch_for(slug)
            if not req.get("anyway") and (b.state or {}).get("status") == "running":
                return self._json({"ok": False, "busy": True,
                                   "error": "正在出片，视觉模型和采样器抢同一块 GPU；"
                                            "等这一章出完再查，或传 anyway:true 强行查"}, code=409)
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str)]
            r = lipcheck_chapter(slug, n, keys or None, bool(req.get("force")))
            r["items"] = lip_items(slug, n)
            return self._json(r)

        if path == "/api/previz/build":
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str)]
            r = build_previz(slug, int(req.get("n") or 1), keys or None,
                             str(req.get("format") or "16:9"))
            if not r.get("ok"):
                return self._json(r, code=400)
            r["items"] = previz_items(slug, int(req.get("n") or 1))
            return self._json(r)
        if path in ("/api/sfx/start", "/api/sfx/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            sb = sfx_batch_for(slug)
            if path.endswith("stop"):
                sb.stop()
                return self._json({"ok": True, "batch": sb.snapshot()})
            ids = [str(i) for i in (req.get("ids") or []) if isinstance(i, str)]
            redo = bool(req.get("redo"))
            if not ids:
                ids = [r["id"] for r in sfx_items(slug)
                       if (redo or not r["exists"]) and not r["hand"]]
            if not ids:
                return self._json({"ok": False, "error": "没有要出的音效"}, code=400)
            r = sb.start(ids, redo)
            r["batch"] = sb.snapshot()
            return self._json(r)
        if path in ("/api/music/start", "/api/music/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            mb = music_batch_for(slug)
            if path.endswith("stop"):
                mb.stop()
                return self._json({"ok": True, "batch": mb.snapshot()})
            ids = [str(i) for i in (req.get("ids") or []) if isinstance(i, str)]
            redo = bool(req.get("redo"))
            if not ids:
                rows = music_items(slug)
                ids = [r["id"] for r in rows if redo or not r["exists"]]
            if not ids:
                return self._json({"ok": False, "error": "没有要出的配乐"}, code=400)
            r = mb.start(ids, redo)
            r["batch"] = mb.snapshot()
            return self._json(r)
        if path == "/api/casting":
            # Casting is config, not a render: writing it costs nothing and
            # takes effect on the next shot the queue picks up, so a voice can
            # be changed while a chapter is rendering.
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            have = set(_voice_pairs())
            cur = _casting(slug)
            nar = str(req.get("narrator") or "").strip()
            if "narrator" in req:
                # "" clears it back to the launch.sh default, which is a real
                # choice and must not be confused with "not supplied".
                if nar and nar not in have:
                    return self._json({"ok": False, "error": "没有这个声音：%s" % nar}, code=400)
                cur["narrator"] = nar
            chars = req.get("characters")
            if isinstance(chars, dict):
                out = dict(cur["characters"])
                for cid, v in chars.items():
                    v = str(v or "").strip()
                    if not v:
                        out.pop(cid, None)          # back to the narrator
                        continue
                    if v not in have:
                        return self._json({"ok": False, "error": "没有这个声音：%s" % v}, code=400)
                    out[cid] = v
                cur["characters"] = out
            os.makedirs(os.path.dirname(casting_path(slug)), exist_ok=True)
            tmp = casting_path(slug) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=1)
            os.replace(tmp, casting_path(slug))
            return self._json({"ok": True, "casting": cur})
        if path in ("/api/video/start", "/api/video/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            vb = video_batch_for(slug)
            if path.endswith("stop"):
                vb.stop()
                return self._json({"ok": True, "batch": vb.snapshot()})
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str) and ":" in k]
            if not keys:
                keys = _all_shot_keys(slug, bool(req.get("redo")))
            if not keys:
                return self._json({"ok": False, "error": "没有要出的镜头"}, code=400)
            r = vb.start(keys, front=bool(req.get("front")), fmt=req.get("format"))
            r["batch"] = vb.snapshot()
            return self._json(r)
        if path == "/api/film/build":
            # Joining is seconds of CPU, so it answers inline rather than
            # earning a queue of its own. `n` omitted means every chapter that
            # has clips; `force` rebuilds one that is already current.
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            force = bool(req.get("force"))
            try:
                ns = [int(req["n"])] if req.get("n") else [r["n"] for r in chapter_rows(slug) if r["clips"]]
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "n 不合法"}, code=400)
            built, errs = [], []
            for n in ns:
                try:
                    built.append({"n": n, **build_chapter_film(slug, n, force=force)})
                except Exception as exc:                    # noqa: BLE001
                    errs.append({"n": n, "error": str(exc)[:200]})
            return self._json({"ok": not errs or bool(built), "built": built, "errors": errs,
                               "chapters": chapter_rows(slug)})
        if path == "/api/stop-all":
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                req = {}
            slug = _safe_slug(str(req.get("slug") or "")) or None
            r = stop_everything(slug, bool(req.get("hard")))
            print("[stop-all] %s" % json.dumps(r, ensure_ascii=False)[:300], flush=True)
            return self._json(r)
        if path in ("/api/lipsync/start", "/api/lipsync/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            lb = lipsync_batch_for(slug)
            if path.endswith("stop"):
                lb.stop()
                return self._json({"ok": True, "batch": lb.snapshot()})
            if not _lipsync_py():
                return self._json({"ok": False, "error": "venvs/lipsync 不存在：LatentSync 没装"}, code=400)
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str) and ":" in k]
            if not keys:
                keys = _lipsync_keys(slug, bool(req.get("redo")))
            if not keys:
                return self._json({"ok": False, "error": "没有需要同步口型的成片（有台词、已出片、还没同步过）"}, code=400)
            r = lb.start(keys, front=bool(req.get("front")), redo=bool(req.get("redo")))
            if (video_batch_for(slug).state or {}).get("status") == "running":
                r["note"] = "正在出片：LatentSync 和采样器会抢同一块 GPU，两边都会慢"
            r["batch"] = lb.snapshot()
            return self._json(r)
        if path in ("/api/upscale/start", "/api/upscale/stop"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            ub = upscale_batch_for(slug)
            if path.endswith("stop"):
                ub.stop()
                return self._json({"ok": True, "batch": ub.snapshot()})
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str) and ":" in k]
            if not keys:
                keys = _all_4k_keys(slug, bool(req.get("redo")))
            if not keys:
                return self._json({"ok": False, "error": "没有要放大的成片"}, code=400)
            r = ub.start(keys, front=bool(req.get("front")))
            r["batch"] = ub.snapshot()
            return self._json(r)
        if path == "/api/voice/upload":
            # raw body, like /api/upload; ?filename= only for its extension
            q = parse_qs(urlparse(self.path).query)
            ext = os.path.splitext(q.get("filename", ["a.wav"])[0])[1].lower()[:6] or ".wav"
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if not 0 < n <= 300 * 1024 * 1024:
                return self._json({"ok": False, "error": "文件为空或超过 300MB"}, code=400)
            body = self.rfile.read(n)
            os.makedirs(VOICE_SRC, exist_ok=True)
            vid = "%016x" % random.getrandbits(64)
            raw = os.path.join(VOICE_SRC, vid + ".orig" + ext)
            with open(raw, "wb") as f:
                f.write(body)
            wav = os.path.join(VOICE_SRC, vid + ".wav")
            try:
                _ff(["-i", raw, "-vn", "-ar", "24000", "-ac", "1", "-c:a", "pcm_f32le", wav], timeout=600)
            except Exception as exc:                        # noqa: BLE001
                return self._json({"ok": False, "error": "读不了这个文件：%s" % str(exc)[:160]}, code=400)
            runs, dur = _speech_runs(wav)
            return self._json({"ok": True, "id": vid, "duration": round(dur, 2),
                               "url": "/api/voice/src?id=" + vid,
                               "segments": _voice_segments(runs), "runs": len(runs)})
        if path == "/api/voice/delete":
            return self._voice_delete()
        if path == "/api/voice/work":
            # POST {name, work}: which 作品 this voice comes from ("" = 未归类)
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            names = req.get("names") or [req.get("name")]
            have = set(_voice_pairs())
            bad = [n for n in names if n not in have]
            if bad:
                return self._json({"ok": False, "error": "声音库里没有 %s" % "、".join(map(str, bad))}, code=404)
            for n in names:
                _set_voice_work(n, req.get("work"))
            return self._json({"ok": True, "works": {n: _voice_work(n) for n in names}})
        if path in ("/api/voice/transcribe", "/api/voice/save"):
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            src = _voice_src(req.get("id"))
            if not src:
                return self._json({"ok": False, "error": "找不到上传的录音，重新上传"}, code=404)
            try:
                start, end = float(req.get("start")), float(req.get("end"))
            except (TypeError, ValueError):
                return self._json({"ok": False, "error": "起止时间不对"}, code=400)
            if path.endswith("transcribe"):
                tmp = os.path.join(VOICE_SRC, "_cut_%x.wav" % random.getrandbits(32))
                try:
                    _voice_cut(src, start, end, tmp, norm=False)
                    boundary = "----bookreel%08x" % random.getrandbits(32)
                    with open(tmp, "rb") as f:
                        data = f.read()
                    mp = (("--%s\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"cut.wav\"\r\n"
                           "Content-Type: audio/wav\r\n\r\n" % boundary).encode() + data +
                          ("\r\n--%s--\r\n" % boundary).encode())
                    r = urllib.request.Request("http://127.0.0.1:9101/transcribe", data=mp,
                                               headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
                    with urllib.request.urlopen(r, timeout=300) as resp:
                        got = json.load(resp)
                except Exception as exc:                    # noqa: BLE001
                    return self._json({"ok": False, "error": "转写失败：%s" % str(exc)[:160]}, code=503)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                return self._json({"ok": True, "text": got.get("text", ""), "tags": got.get("tags", [])})
            name = str(req.get("name") or "").strip()
            text = " ".join(str(req.get("text") or "").split())
            if not _VOICE_NAME_RX.match(name) or name.startswith("_"):
                return self._json({"ok": False, "error": "名字只能用字母、数字、下划线、横线或汉字，不能以 _ 开头"}, code=400)
            if not text:
                return self._json({"ok": False, "error": "要有这段录音的逐字稿，克隆靠它对齐"}, code=400)
            if not 3.0 <= end - start <= 15.0:
                return self._json({"ok": False, "error": "声音样本要 3-15 秒，现在是 %.1f 秒" % (end - start)}, code=400)
            dst = os.path.join(ROOT, "voices", name + ".wav")
            if os.path.exists(dst) and not req.get("overwrite"):
                return self._json({"ok": False, "exists": True, "error": "已有同名声音 %s" % name}, code=409)
            try:
                _voice_cut(src, start, end, dst + ".part.wav")
                os.replace(dst + ".part.wav", dst)
            except Exception as exc:                        # noqa: BLE001
                return self._json({"ok": False, "error": "剪切失败：%s" % str(exc)[:160]}, code=500)
            with open(os.path.join(ROOT, "voices", name + ".txt"), "w", encoding="utf-8") as f:
                f.write(text)
            if str(req.get("work") or "").strip():
                _set_voice_work(name, req.get("work"))
            try:
                os.unlink(os.path.join(ROOT, "voices", "_previews", name + ".mp3"))  # old audition
            except OSError:
                pass
            return self._json({"ok": True, "name": name, "seconds": round(end - start, 2)})
        if path == "/api/prompt":
            return self._prompt_edit()
        if path == "/api/approve":
            return self._approve()
        if path == "/api/note":
            # POST /api/note {slug, key, note} - what is wrong with this picture,
            # in the person's words. Kept on the item and put at the front of
            # every render of it until cleared; "" clears it.
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            key = str(req.get("key") or "")
            note = str(req.get("note") or "").strip()[:500]
            if req.get("kind") == "clip":
                if not slug or not re.match(r"^\d+:[A-Za-z0-9_]+$", key):
                    return self._json({"ok": False, "error": "key 应为 章:镜号"}, code=400)
                with _PROMPTS_LOCK:
                    notes = _read_json(_clip_notes_path(slug), {}) or {}
                    if note:
                        notes[key] = note
                    else:
                        notes.pop(key, None)
                    os.makedirs(os.path.dirname(_clip_notes_path(slug)), exist_ok=True)
                    with open(_clip_notes_path(slug) + ".tmp", "w", encoding="utf-8") as f:
                        json.dump(notes, f, ensure_ascii=False, indent=1)
                    os.replace(_clip_notes_path(slug) + ".tmp", _clip_notes_path(slug))
                return self._json({"ok": True, "key": key, "note": note})
            path_ = os.path.join(PROJECTS, slug, "assets", "prompts.json")
            with _PROMPTS_LOCK:
                lib = _read_json(path_, {}) or {}
                it = next((i for i in lib.get("items", []) if i.get("key") == key), None)
                if not it:
                    return self._json({"ok": False, "error": "没有这张图"}, code=404)
                if note:
                    it["user_note"] = note
                else:
                    it.pop("user_note", None)
                tmp = path_ + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(lib, f, ensure_ascii=False, indent=2)
                os.replace(tmp, path_)
            return self._json({"ok": True, "key": key, "note": note})
        if path == "/api/batch/skip":
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
            except (ValueError, OSError):
                return self._json({"ok": False, "error": "bad json"}, code=400)
            slug = _safe_slug(str(req.get("slug") or ""))
            keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str)]
            if not slug or not keys:
                return self._json({"ok": False, "error": "slug/keys"}, code=400)
            b = batch_for(slug)
            return self._json({"ok": True, "skipped": b.skip(keys, interrupt=not req.get("queued_only")),
                               "batch": b.snapshot()})
        return self._save()

    def _autorun_load(self, preview=False):
        """POST /api/autorun/load?title=&era=&style=&notes=&source=&part_chars=
        with the novel file as the raw body: make the project, keep the text,
        start the run. /api/autorun/preview takes the same body and only says
        how the book splits (chapters -> parts), without creating anything."""
        q = parse_qs(urlparse(self.path).query)

        def arg(k):
            return (q.get(k) or [""])[0].strip()
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 64 * 1024 * 1024:
            return self._json({"ok": False, "error": "文件为空或超过 64MB"}, code=400)
        text = _decode_novel(self.rfile.read(n))
        if text is None:
            return self._json({"ok": False, "error": "读不出这个文件的编码（支持 UTF-8 / GBK / Big5 的 .txt）"}, code=400)
        text = text.strip()
        if len(text) < 200:
            return self._json({"ok": False, "error": "正文不到 200 字"}, code=400)
        title = arg("title")[:80] or "未命名小说"
        try:
            part_chars = max(600, min(4000, int(arg("part_chars") or 1500)))
        except ValueError:
            part_chars = 1500
        try:
            dry = _story_post("/import", {"slug": "preview", "text": text, "dry_run": True,
                                          "part_chars": part_chars}, 300)
        except RuntimeError as exc:
            return self._json({"ok": False, "error": str(exc)}, code=502)
        if preview:
            return self._json({"ok": True, "title": title, "chars": dry.get("chars"),
                               "parts": dry.get("parts"), "books": dry.get("books") or []})
        busy = [a.slug for a in _AUTORUN.values() if a.thread and a.thread.is_alive()]
        if busy:
            return self._json({"ok": False, "error": "已有一部小说在自动流程里（%s）；等它结束或先停下"
                               % "、".join(busy)}, code=409)
        slug = _novel_slug(title)
        src = os.path.join(PROJECTS, slug, "source")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "original.txt"), "w", encoding="utf-8") as f:
            f.write(text)
        opts = {"title": title, "era": arg("era"), "style": arg("style"), "notes": arg("notes"),
                "source": arg("source"), "part_chars": part_chars}
        ar = autorun_for(slug)
        ar.state["books"] = dry.get("books") or []
        ar.state["parts"] = dry.get("parts") or 0
        ar._log("载入《%s》：%d 字，原书 %d 章 → %d 段" % (
            title, dry.get("chars") or 0, len(dry.get("books") or []), dry.get("parts") or 0))
        ar.start(opts)
        return self._json({"ok": True, "slug": slug, "run": ar.snapshot()})

    def _voice_delete(self):
        """POST /api/voice/delete {name, force?} - take a voice out of the library.

        The files are MOVED to voices/_trash/<name>-<time>/, never erased: the
        pairs are not in git, and a reference clip somebody recorded is not
        something to lose to a misclick. The default narrator (BOOKREEL_VOICE)
        is refused - it is what every uncast line falls back to. A voice a
        project still casts is refused too, naming who uses it, unless
        force: then those roles go back to the narrator (casting's own "")."""
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)
        name = str(req.get("name") or "").strip()
        if name not in _voice_pairs():
            return self._json({"ok": False, "error": "声音库里没有 %s" % name}, code=404)
        if name == VOICE:
            return self._json({"ok": False, "error": "%s 是默认旁白（BOOKREEL_VOICE），没定音色的台词都靠它，不能删" % name},
                              code=409)
        used = []
        # every project that HAS a casting, blueprint or not
        for slug in sorted(os.listdir(PROJECTS)) if os.path.isdir(PROJECTS) else []:
            if not os.path.isfile(casting_path(slug)):
                continue
            c = _casting(slug)
            if c["narrator"] == name:
                used.append({"slug": slug, "role": "旁白"})
            names = {x.get("id"): x.get("name") for x in (_read_json(
                os.path.join(PROJECTS, slug, "blueprint.json"), {}) or {}).get("characters") or []}
            for cid, v in c["characters"].items():
                if v == name:
                    used.append({"slug": slug, "role": ("%s %s" % (cid, names[cid])) if names.get(cid) else cid})
        if used and not req.get("force"):
            return self._json({"ok": False, "used": used,
                               "error": "还在用：" + "、".join("%s/%s" % (u["slug"], u["role"]) for u in used)},
                              code=409)
        for slug in sorted({u["slug"] for u in used}):
            with _PROMPTS_LOCK:
                cur = _read_json(casting_path(slug), {}) or {}
                if cur.get("narrator") == name:
                    cur["narrator"] = ""
                chars = cur.get("characters") if isinstance(cur.get("characters"), dict) else {}
                cur["characters"] = {k: v for k, v in chars.items() if v != name}
                with open(casting_path(slug) + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(cur, f, ensure_ascii=False, indent=1)
                os.replace(casting_path(slug) + ".tmp", casting_path(slug))
        vdir = os.path.join(ROOT, "voices")
        trash = os.path.join(vdir, "_trash", "%s-%s" % (name, time.strftime("%Y%m%d-%H%M%S")))
        os.makedirs(trash, exist_ok=True)
        moved = []
        for fn in [name + ext for ext in (".wav", ".txt", ".rhythm", ".json")] + [os.path.join("_previews", name + ".mp3")]:
            src = os.path.join(vdir, fn)
            if os.path.isfile(src):
                shutil.move(src, os.path.join(trash, os.path.basename(fn)))
                moved.append(fn)
        print("[web] voice %s -> %s (%s)%s" % (name, trash, ", ".join(moved),
              ("；改回旁白：" + "、".join("%s/%s" % (u["slug"], u["role"]) for u in used)) if used else ""),
              flush=True)
        return self._json({"ok": True, "name": name, "trash": os.path.relpath(trash, ROOT),
                           "moved": moved, "recast": used})

    def _prompt_edit(self):
        """POST /api/prompt {slug, key, prompt?, negative?, reset?}

        The person's own prompt / negative for one image, kept on the item as
        prompt_user / negative_user and used verbatim by every render of it.
        A text equal to what the system would send anyway is not an override
        (so saving without changing anything changes nothing); reset: true, or
        an empty prompt, goes back to the automatic one. /assets carries these
        over the way it carries 重出意见."""
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)
        slug = _safe_slug(str(req.get("slug") or ""))
        key = str(req.get("key") or "")
        path_ = os.path.join(PROJECTS, slug, "assets", "prompts.json")
        with _PROMPTS_LOCK:
            lib = _read_json(path_, {}) or {}
            it = next((i for i in lib.get("items") or [] if i.get("key") == key), None)
            if not it:
                return self._json({"ok": False, "error": "没有 %s" % key}, code=404)
            if req.get("reset"):
                it.pop("prompt_user", None)
                it.pop("negative_user", None)
            else:
                if "prompt" in req:
                    t = str(req.get("prompt") or "").strip()[:8000]
                    if not t or t == sheet_prompt(it, auto=True):
                        it.pop("prompt_user", None)
                    else:
                        it["prompt_user"] = t
                if "negative" in req:
                    t = ", ".join(x.strip() for x in re.split(r"[,，\n]", str(req.get("negative") or "")) if x.strip())[:8000]
                    if t == sheet_negative(it, auto=True):
                        it.pop("negative_user", None)
                    else:
                        it["negative_user"] = t
            with open(path_ + ".tmp", "w", encoding="utf-8") as f:
                json.dump(lib, f, ensure_ascii=False, indent=2)
            os.replace(path_ + ".tmp", path_)
        return self._json({"ok": True, "key": key,
                           "prompt": sheet_prompt(it), "negative": sheet_negative(it),
                           "prompt_edited": bool(it.get("prompt_user")),
                           "negative_edited": it.get("negative_user") is not None,
                           "negative_read": sheet_cfg(it) > 1.0})

    def _approve(self):
        """POST /api/approve {slug, key, kind:'image'|'clip', undo?} - 人工认可.

        The person looked and it is right, whatever the checker said: no
        re-check, no re-render, no lip-sync redo. Written onto the LIVE take,
        so choosing a different take is a fresh decision.
        """
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8") or "{}")
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)
        slug = _safe_slug(str(req.get("slug") or ""))
        key = str(req.get("key") or "")
        undo = bool(req.get("undo"))
        if not slug or not key:
            return self._json({"ok": False, "error": "slug/key"}, code=400)
        if req.get("kind") == "clip":
            try:
                n, sid = key.split(":", 1)
                n = int(n)
            except ValueError:
                return self._json({"ok": False, "error": "key 应为 章:镜号"}, code=400)
            takes = os.path.join(PROJECTS, slug, "clips", "%02d" % n, "_takes", sid)
            t = (_read_json(os.path.join(takes, "_active.json"), {}) or {}).get("take")
            meta_p = os.path.join(takes, "%s.json" % t)
            if not t or not os.path.isfile(meta_p):
                return self._json({"ok": False, "error": "这一镜还没有片段"}, code=404)
            meta = _read_json(meta_p, {}) or {}
            if undo:
                meta.pop("approved", None)
            else:
                meta["approved"] = {"at": int(time.time())}
            with open(meta_p, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            if not undo:
                lp = lip_path(slug, n, sid)
                lip = _read_json(lp, {}) or {}
                if lip:
                    lip.update({"ok": True, "manual": True, "note": "人工认可"})
                    with open(lp, "w", encoding="utf-8") as f:
                        json.dump(lip, f, ensure_ascii=False)
                for q in (lipsync_batch_for(slug), video_batch_for(slug)):
                    with q.lock:
                        if key in (q.state.get("queue") or []):
                            q.state["queue"] = [k for k in q.state["queue"] if k != key]
                            q._save()
            return self._json({"ok": True, "key": key, "approved": not undo})
        item = next((i for i in (_read_json(os.path.join(PROJECTS, slug, "assets", "prompts.json"), {}) or {}).get("items", [])
                     if i.get("key") == key), None)
        if not item:
            return self._json({"ok": False, "error": "没有这张图"}, code=404)
        full = os.path.join(PROJECTS, slug, "assets", "images", item["file"])
        n = _take_active(full)
        if not n or not os.path.isfile(full):
            return self._json({"ok": False, "error": "这张图还没有生成"}, code=404)
        meta = _read_json(os.path.join(_take_dir(full)[0], "%d.json" % n), {}) or {}
        if undo:
            cur = meta.get("check") or {}
            if cur.get("manual"):
                # back to what the checker said, not to "never checked"
                _take_note(full, n, {"check": meta.get("check_before"), "check_before": None})
            else:
                # revoking the CHECKER's pass: every 认可 can be taken back
                # the checker's verdict travels INSIDE the revoke record, so no
                # later approve/undo can overwrite it
                _take_note(full, n, {"check": {"ok": False, "revoked": True, "was": cur or None,
                                               "problems": ["人工撤销认可"], "fix": "", "answers": {},
                                               "at": int(time.time())}})
        else:
            before = meta.get("check")
            if (before or {}).get("manual"):
                before = meta.get("check_before")
            _take_note(full, n, {"check_before": before,
                                 "check": {"ok": True, "manual": True, "problems": [], "fix": "",
                                           "answers": {}, "note": "人工认可", "at": int(time.time())}})
            batch_for(slug).skip([key])
        return self._json({"ok": True, "key": key, "approved": not undo})

    def _batch(self, stop):
        """POST /api/batch/start {slug, keys:[...], mode:'first'|'redo'}; POST /api/batch/stop {slug}"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)
        slug = _safe_slug(str(req.get("slug") or ""))
        if not slug:
            return self._json({"ok": False, "error": "slug 不合法"}, code=400)
        b = batch_for(slug)
        if stop:
            b.stop()
            return self._json({"ok": True, "batch": b.snapshot()})
        keys = [str(k) for k in (req.get("keys") or []) if isinstance(k, str) and 0 < len(k) < 120]
        if not keys:
            return self._json({"ok": False, "error": "没有要出的图"}, code=400)
        mode = req.get("mode") if req.get("mode") in ("redo", "check") else "first"
        if len(keys) > 1:
            # A bulk button (全部重出, 四面全部重出) is not a verdict on the pictures
            # that already passed: 2026-09-25 it put an approved char_ch06 and three
            # ✓ 已核 sheets back in line, 12 min each. Only a click on ONE tile
            # redraws a picture that passed.
            passed = set(_passed_keys(slug, keys))
            keys = [k for k in keys if k not in passed]
            if not keys:
                return self._json({"ok": True, "queued": 0, "skipped_passed": len(passed),
                                   "batch": b.snapshot()})
        else:
            passed = set()
        r = b.start(keys, mode, front=bool(req.get("front")))
        r["skipped_passed"] = len(passed)
        r["batch"] = b.snapshot()
        return self._json(r)

    def _upload(self):
        """POST /api/upload?slug=&path= with the file as the raw body.

        /api/save pulls from a URL, which covers ComfyUI and h3_server because
        both hand out http links. Audio synthesised in the page has no URL the
        server can reach -- a blob: handle means nothing outside that tab -- so
        the bytes come up in the body instead.
        """
        q = parse_qs(urlparse(self.path).query)
        slug = _safe_slug((q.get("slug") or [""])[0])
        rel = (q.get("path") or [""])[0]
        if not slug or not self.SAVE_PATH.match(rel):
            return self._json({"ok": False, "error": "slug 或 path 不合法"}, code=400)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not 0 < n <= 64 * 1024 * 1024:
            return self._json({"ok": False, "error": "体积为 0 或超过 64MB"}, code=400)
        body = self.rfile.read(n)
        full = os.path.join(PROJECTS, slug, *rel.split("/"))
        if not os.path.realpath(full).startswith(
                os.path.realpath(os.path.join(PROJECTS, slug)) + os.sep):
            return self._json({"ok": False, "error": "路径越界"}, code=400)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, full)
        return self._json({"ok": True, "bytes": len(body),
                           "url": "/media/%s/%s" % (slug, rel)})

    def _save(self):
        """POST /api/save {slug, path, url} -- pull a rendered file into the project.

        ComfyUI hands the page a /view?filename=... URL and nothing else; the
        image lives in ComfyUI's own output directory and is invisible to the
        project. The browser cannot write to disk, so the server fetches it and
        files it under projects/<slug>/. Without this the Library page reports
        "0 图" no matter how much was rendered.
        """
        if unquote(urlparse(self.path).path) != "/api/save":
            return self.send_error(404, "Not found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)

        slug = _safe_slug(str(req.get("slug") or ""))
        rel = str(req.get("path") or "")
        url = str(req.get("url") or "")
        if not slug or not self.SAVE_PATH.match(rel):
            return self._json({"ok": False, "error": "slug 或 path 不合法"}, code=400)
        # Only ever fetch from this machine. The URL comes from the page, and a
        # server that will fetch any URL on request is a proxy for whoever can
        # reach it -- bound to 0.0.0.0, that is the whole network.
        u = urlparse(url)
        if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in (
                "127.0.0.1", "localhost", "::1", (_lan_ip() or "").lower()):
            return self._json({"ok": False, "error": "只允许从本机地址取文件"}, code=400)

        full = os.path.join(PROJECTS, slug, *rel.split("/"))
        if not os.path.realpath(full).startswith(
                os.path.realpath(os.path.join(PROJECTS, slug)) + os.sep):
            return self._json({"ok": False, "error": "路径越界"}, code=400)
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                body = r.read()
        except (urllib.error.URLError, OSError) as exc:
            return self._json({"ok": False, "error": "取文件失败: %s" % exc}, code=502)
        if not body:
            return self._json({"ok": False, "error": "取到 0 字节"}, code=502)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if rel.startswith("assets/images/"):
            meta = req.get("meta") if isinstance(req.get("meta"), dict) else {}
            meta = {k: v for k, v in meta.items()
                    if isinstance(k, str) and isinstance(v, (int, float, str)) and len(str(v)) < 200}
            n = _take_add(full, body, meta)
            return self._json({"ok": True, "bytes": len(body), "take": n,
                               "takes": len(_take_list(full)),
                               "url": "/media/%s/%s?t=%d" % (slug, rel, int(os.path.getmtime(full)))})
        tmp = full + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, full)
        return self._json({"ok": True, "bytes": len(body),
                           "url": "/media/%s/%s" % (slug, rel)})

    def _take(self, delete=False):
        """POST /api/take {slug, path, take}        -- make one earlier render the live one.
           POST /api/take/delete {slug, path, take} -- remove one render."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, OSError):
            return self._json({"ok": False, "error": "bad json"}, code=400)
        slug = _safe_slug(str(req.get("slug") or ""))
        rel = str(req.get("path") or "")
        if not slug or not self.SAVE_PATH.match(rel) or not (
                rel.startswith("assets/images/") or rel.startswith("clips/")):
            return self._json({"ok": False, "error": "slug 或 path 不合法"}, code=400)
        try:
            take = int(req.get("take"))
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "take 要是整数"}, code=400)
        full = os.path.join(PROJECTS, slug, *rel.split("/"))
        if not os.path.realpath(full).startswith(
                os.path.realpath(os.path.join(PROJECTS, slug)) + os.sep):
            return self._json({"ok": False, "error": "路径越界"}, code=400)
        _take_adopt(full)
        if delete:
            rest = _take_delete(full, take)
            if rest is None:
                return self._json({"ok": False, "error": "没有第 %d 次出图" % take}, code=404)
            return self._json({"ok": True, "deleted": take, "takes": _takes_for(slug, rel, full),
                               "exists": os.path.isfile(full)})
        if not _take_select(full, take):
            return self._json({"ok": False, "error": "没有第 %d 次出图" % take}, code=404)
        return self._json({"ok": True, "take": take, "takes": _takes_for(slug, rel, full),
                           "url": "/media/%s/%s?t=%d" % (slug, rel, int(os.path.getmtime(full)))})

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        rel = posixpath.normpath(path).lstrip("/")
        if path == "/gpu":
            return self._json(gpu_info())
        if path == "/api/projects":
            return self._json(projects_index())
        if path == "/api/video":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            return self._json(video_batch_for(slug).snapshot() if slug else {"status": "idle"})
        if path == "/api/srt":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            try:
                n = int(q.get("n", ["0"])[0])
            except ValueError:
                n = 0
            if not slug or not n:
                return self._json({"ok": False, "error": "需要 slug 与 n"}, code=400)
            # film=1 asks for the subtitles of the FILM, which starts with a
            # title card, rather than of the bare clip sequence.
            off = FILM_TITLE_SECONDS if q.get("film", ["0"])[0] not in ("0", "", "false") else 0.0
            body, info = chapter_srt(slug, n, offset=off)
            return self._json({"ok": True, "srt": body, "srt_name": "chapter-%02d.srt" % n,
                               **info})
        if path == "/api/chapters":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            return self._json({"ok": True, "chapters": chapter_rows(slug),
                               "title_seconds": FILM_TITLE_SECONDS})
        if path == "/api/vtt":
            # The player wants WebVTT; everything else here speaks SRT. The two
            # differ by a header and a decimal point.
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            try:
                n = int(q.get("n", ["0"])[0])
            except ValueError:
                n = 0
            if not slug or not n:
                return self._json({"ok": False, "error": "需要 slug 与 n"}, code=400)
            srt, _info = chapter_srt(slug, n, offset=FILM_TITLE_SECONDS)
            body = ("WEBVTT\n\n" + re.sub(r"(\d\d:\d\d:\d\d),(\d\d\d)", r"\1.\2", srt)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return self._body(body)
        if path == "/api/lipsync":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            return self._json({"ok": True, "installed": bool(_lipsync_py()),
                               "pending": _lipsync_keys(slug),
                               "batch": lipsync_batch_for(slug).snapshot()})
        if path == "/api/upscale":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"status": "idle"})
            st = upscale_batch_for(slug).snapshot()
            st["pending"] = len(_all_4k_keys(slug))
            st["model"] = os.path.basename(UPSCALE_MODEL)
            st["model_present"] = os.path.isfile(UPSCALE_MODEL)
            st["target"] = UPSCALE_TARGET
            return self._json(st)
        if path == "/api/clip-notes":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            return self._json({"ok": True, "notes": _read_json(_clip_notes_path(slug), {}) or {}})
        if path in ("/api/voice/src", "/api/voice/clip"):
            q = parse_qs(urlparse(self.path).query)
            src = _voice_src(q.get("id", [""])[0])
            if not src:
                return self.send_error(404, "no such recording")
            f = src
            if path.endswith("clip"):
                f = os.path.join(VOICE_SRC, "_play_%x.wav" % random.getrandbits(32))
                try:
                    _voice_cut(src, q.get("start", ["0"])[0], q.get("end", ["1"])[0], f, norm=False)
                except Exception as exc:                    # noqa: BLE001
                    return self.send_error(400, str(exc)[:100])
            try:
                with open(f, "rb") as fh:
                    data = fh.read()
            finally:
                if f != src:
                    try:
                        os.unlink(f)
                    except OSError:
                        pass
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path in ("/api/voice/line", "/api/voice/line.mp3"):
            # A character's own line from the novel, in the voice cast for them.
            # /api/voice/line -> {text, source, voice, url}; the .mp3 is the audio.
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            cid = str(q.get("cid", [""])[0])
            if not slug or not re.match(r"^[A-Za-z0-9_]+$", cid):
                return self._json({"ok": False, "error": "slug/cid"}, code=400)
            ln = character_line(slug, cid)
            if not ln:
                return self._json({"ok": False, "error": "分镜和原文里都没找到这个人物说的话"}, code=404)
            have = set(_voice_pairs())
            voice = _voice_for(cid, _casting(slug), have)
            ver = _voice_version(voice)
            h = hashlib.sha1((voice + "|" + ln["text"]).encode("utf-8")).hexdigest()[:12]
            if path.endswith("line"):
                return self._json({"ok": True, "text": ln["text"], "source": ln["source"], "voice": voice,
                                   "url": "/api/voice/line.mp3?slug=%s&cid=%s&h=%s&v=%d"
                                          % (urllib.parse.quote(slug), cid, h, ver)})
            dst = os.path.join(ROOT, "voices", "_previews", "lines", "%s-%s.mp3" % (voice, h))
            if not os.path.isfile(dst) or os.path.getmtime(dst) < ver:
                try:
                    data = _synth(ln["text"], voice, _instruct_for(ln.get("delivery") or {}), timeout=600)
                except Exception as exc:                    # noqa: BLE001
                    return self._json({"ok": False, "error": "合成失败：%s" % str(exc)[:120]}, code=503)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                wav = dst + ".wav"
                with open(wav, "wb") as f:
                    f.write(data)
                part = dst[:-4] + ".part.mp3"      # .part.mp3, never .mp3.part
                try:
                    _ff(["-i", wav, "-c:a", "libmp3lame", "-b:a", "128k", part], timeout=120)
                    os.replace(part, dst)
                finally:
                    try:
                        os.unlink(wav)
                    except OSError:
                        pass
            with open(dst, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")   # URL carries voice, line and version
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/voice/sample":
            # One short line in a given voice, cached as mp3. Casting is a
            # listening decision and the picker was useless without it: the
            # names say narrator_m9 / cast_m_old, which tells you nothing about
            # what they sound like. Generated on first ask (CosyVoice runs on
            # CPU here, ~40 s for this line) and instant after that, so the
            # page can offer every voice at once without a minute of silence.
            q = parse_qs(urlparse(self.path).query)
            name = str(q.get("name", [""])[0]).strip()
            if not name or name not in set(_voice_pairs()):
                return self._json({"ok": False, "error": "没有这个声音"}, code=404)
            dst = os.path.join(ROOT, "voices", "_previews", name + ".mp3")
            # an audition older than the voice is the OLD voice: rebuild it
            if not os.path.isfile(dst) or os.path.getmtime(dst) < _voice_version(name):
                try:
                    data = _synth(VOICE_SAMPLE_TEXT, name, "", timeout=600)
                except Exception as exc:                    # noqa: BLE001
                    return self._json({"ok": False, "error": "合成失败：%s" % str(exc)[:120]}, code=503)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                wav = dst + ".wav"
                with open(wav, "wb") as f:
                    f.write(data)
                # <name>.part.mp3, never <name>.mp3.part: ffmpeg picks its
                # muxer off the extension and refuses to initialise one for
                # ".part" ("Error initializing the muxer ... Invalid
                # argument"), which is the same trap the 4K pass fell into.
                part = dst[:-4] + ".part.mp3"
                try:
                    _ff(["-i", wav, "-c:a", "libmp3lame", "-b:a", "128k", part], timeout=120)
                    os.replace(part, dst)
                finally:
                    try:
                        os.unlink(wav)
                    except OSError:
                        pass
            try:
                with open(dst, "rb") as f:
                    body = f.read()
            except OSError:
                return self._json({"ok": False, "error": "读不到试听文件"}, code=500)
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/music":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            st = music_batch_for(slug).snapshot()
            st["ok"] = True
            st["items"] = music_items(slug)
            st["model_present"] = _ace_present()
            st["seconds"] = MUSIC_SECONDS
            return self._json(st)
        if path == "/api/lipcheck":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            try:
                n = int(q.get("n", ["1"])[0])
            except ValueError:
                n = 1
            return self._json({"ok": True, "slug": slug, "n": n,
                               "items": lip_items(slug, n), "model": VERIFY_MODEL})

        if path == "/api/previz":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            try:
                n = int(q.get("n", ["1"])[0])
            except ValueError:
                n = 1
            rooms = _read_json(os.path.join(PROJECTS, slug, "assets", "stage.json"), {}) or {}
            return self._json({"ok": True, "slug": slug, "n": n,
                               "items": previz_items(slug, n),
                               "rooms": sorted(rooms.keys()),
                               "enabled": PREVIZ_ENABLED,
                               "control": PREVIZ_CONTROL,
                               "strength": PREVIZ_STRENGTH,
                               "model_present": _control_model_present()})
        if path == "/api/sfx":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            st = sfx_batch_for(slug).snapshot()
            bp = _read_json(os.path.join(PROJECTS, slug, "blueprint.json"), {}) or {}
            st["ok"] = True
            st["items"] = sfx_items(slug)
            st["model_present"] = _sfx_model_present()
            st["model"] = "Stable Audio Open 1.0"
            st["need"] = [] if _sfx_model_present() else [
                "ComfyUI/models/checkpoints/" + SFX_CKPT,
                "ComfyUI/models/text_encoders/" + SFX_CLIP]
            st["profile"] = look.profile(bp)
            st["dir"] = os.path.relpath(sfx_dir(bp), ROOT)
            return self._json(st)
        if path == "/api/casting":
            # Who is in this story, which voices exist, and who is cast as what.
            # One call so the page can draw the whole casting panel without
            # three round trips, and so a voice that has lost its .txt stops
            # being offered the moment it does.
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            if not slug:
                return self._json({"ok": False, "error": "slug 不合法"}, code=400)
            bp = _read_json(os.path.join(PROJECTS, slug, "blueprint.json"), {}) or {}
            cast = _casting(slug)
            people = []
            for ch in (bp.get("characters") or []):
                cid = str(ch.get("id") or "").strip()
                if not cid:
                    continue
                people.append({"id": cid, "name": ch.get("name") or cid,
                               "gender": ch.get("gender") or "", "age": ch.get("age"),
                               "voice": (cast["characters"] or {}).get(cid) or ""})
            return self._json({"ok": True, "voices": _voice_pairs(),
                               "voice_versions": {v: _voice_version(v) for v in _voice_pairs()},
                               "voice_works": {v: _voice_work(v) for v in _voice_pairs()},
                               "narrator": cast["narrator"] or VOICE,
                               "default_narrator": VOICE, "characters": people})
        if path == "/api/video/plan":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            return self._json(video_plan(slug) if slug else {"shots": []})
        if path == "/api/batch":
            q = parse_qs(urlparse(self.path).query)
            slug = _safe_slug(q.get("slug", [""])[0])
            return self._json(batch_for(slug).snapshot() if slug else {"status": "idle"})
        if path == "/api/chapter":
            q = parse_qs(urlparse(self.path).query)
            data = chapter_text(_safe_slug(q.get("slug", [""])[0]),
                                int((q.get("n", ["1"])[0] or "1")))
            if not data:
                return self.send_error(404, "no such chapter")
            return self._json(data)
        if path == "/api/blueprint":
            q = parse_qs(urlparse(self.path).query)
            bp = blueprint_of(_safe_slug(q.get("slug", [""])[0]))
            if not bp:
                return self.send_error(404, "no such project")
            return self._json(bp)
        if path == "/api/autorun":
            slug = _safe_slug(parse_qs(urlparse(self.path).query).get("slug", [""])[0])
            if slug:
                return self._json(autorun_for(slug).snapshot())
            return self._json({"runs": autorun_list()})
        if path == "/api/library":
            slug = parse_qs(urlparse(self.path).query).get("slug", [""])[0]
            data = library(_safe_slug(slug))
            if not data:
                return self.send_error(404, "no such project")
            return self._json(data)
        if rel.startswith("media/"):
            return self._media(rel[len("media/"):])
        if path in ("/", "/index.html"):
            return self._index()
        if rel == "index.html":
            return self._index()
        if not _allowed(rel):
            return self.send_error(404, "Not found")
        full = os.path.join(ROOT, *rel.split("/"))
        # normpath above kills ../, but symlinks could still point outside.
        if not os.path.realpath(full).startswith(os.path.realpath(ROOT) + os.sep):
            return self.send_error(404, "Not found")
        if not os.path.isfile(full):
            return self.send_error(404, "Not found")
        try:
            with open(full, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_error(404, "Not found")
        ext = os.path.splitext(full)[1].lower()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        # The console is edited constantly during a run; a cached support.js
        # or .dc.html is a confusing way to lose an afternoon.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._body(body)

    def _media(self, rest):
        """projects/<slug>/<path> -- generated images and clips only."""
        parts = [p for p in rest.split("/") if p and p != "."]
        if len(parts) < 2 or any(p == ".." for p in parts):
            return self.send_error(404, "Not found")
        if os.path.splitext(parts[-1])[1].lower() not in MEDIA_EXT:
            return self.send_error(404, "Not found")
        # The sound library is shared across projects, so it lives at the repo
        # root beside voices/ rather than under any one project. One reserved
        # first segment rather than a second handler, so a bed and a door knock
        # are served by the same Range-capable code path.
        root = SFX_DIR if parts[0] == "_sfx" else PROJECTS
        full = os.path.join(root, *parts[1:]) if parts[0] == "_sfx" else os.path.join(PROJECTS, *parts)
        if not os.path.realpath(full).startswith(os.path.realpath(root) + os.sep):
            return self.send_error(404, "Not found")
        if not os.path.isfile(full):
            return self.send_error(404, "Not found")
        ext = os.path.splitext(full)[1].lower()
        try:
            size = os.path.getsize(full)
            # Range, because a chapter film is minutes long: without it the
            # browser cannot seek (it re-fetches from zero, and some players
            # refuse to scrub at all). A 15-second clip never needed this; a
            # 6-minute one does.
            start, end = 0, size - 1
            rng = self.headers.get("Range") or ""
            m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
            partial = False
            if m and size:
                a, b = m.group(1), m.group(2)
                if a:
                    start = min(int(a), size - 1)
                    if b:
                        end = min(int(b), size - 1)
                elif b:                       # bytes=-N, the last N bytes
                    start = max(0, size - int(b))
                partial = True
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
            length = end - start + 1
            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", TYPES.get(ext, "application/octet-stream"))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not getattr(self, "_head_only", False):
                with open(full, "rb") as fh:
                    fh.seek(start)
                    left = length
                    while left > 0:
                        chunk = fh.read(min(1 << 20, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
        except (OSError, ValueError):
            pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # same-origin in normal use, but the console may be served elsewhere
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._body(body)

    def _index(self):
        body = _hub_html().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self._body(body)


# The landing page. Deliberately self-contained -- no design system, no React,
# no CDN -- because it is the one page that must render even when everything
# else is half-installed or switched off to free VRAM.
def _run(argv, timeout=4):
    """Best-effort command capture. Returns '' rather than raising: a missing
    vendor tool is the normal case on any machine that lacks that vendor."""
    if not shutil.which(argv[0]):
        return ""
    try:
        out = subprocess.run(argv, capture_output=True, timeout=timeout)
        return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _gpu_amd():
    raw = _run(["rocm-smi", "--showuse", "--showmeminfo", "vram",
                "--showproductname", "--json"])
    if not raw:
        return []
    try:
        cards = json.loads(raw)
    except ValueError:
        return []
    out = []
    for key in sorted(cards):
        c = cards[key]
        name = (c.get("Card Series") or c.get("Card Model") or key).split("[")[0].strip() or key
        gfx = c.get("GFX Version")
        if gfx:
            name = "%s (%s)" % (name, gfx)

        def num(k):
            try:
                return int(c[k])
            except (KeyError, TypeError, ValueError):
                return None
        total, used, use = (num("VRAM Total Memory (B)"),
                            num("VRAM Total Used Memory (B)"), num("GPU use (%)"))
        out.append({"name": name, "util": use,
                    "mem_used_gb": round(used / 1e9, 1) if used is not None else None,
                    "mem_total_gb": round(total / 1e9, 1) if total is not None else None})
    return out


def _gpu_nvidia():
    raw = _run(["nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits"])
    out = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue

        def num(v):
            try:
                return float(v)
            except ValueError:
                return None
        util, used, total = num(parts[1]), num(parts[2]), num(parts[3])
        out.append({"name": parts[0],
                    "util": int(util) if util is not None else None,
                    "mem_used_gb": round(used * 1048576 / 1e9, 1) if used is not None else None,
                    "mem_total_gb": round(total * 1048576 / 1e9, 1) if total is not None else None})
    return out


def _gpu_macos():
    """Apple Silicon has no separate VRAM; report unified memory instead."""
    if sys.platform != "darwin":
        return []
    name = ""
    for line in _run(["system_profiler", "SPDisplaysDataType"], timeout=8).splitlines():
        if "Chipset Model:" in line:
            name = line.split(":", 1)[1].strip()
            break
    mem = _run(["sysctl", "-n", "hw.memsize"]).strip()
    total = round(int(mem) / 1e9, 1) if mem.isdigit() else None
    return [{"name": name or "Apple Silicon", "util": None,
             "mem_used_gb": None, "mem_total_gb": total, "unified": True}]


def gpu_info():
    for src, fn in (("rocm-smi", _gpu_amd), ("nvidia-smi", _gpu_nvidia), ("macos", _gpu_macos)):
        gpus = fn()
        if gpus:
            break
    else:
        src, gpus = "none", []
    try:
        du = shutil.disk_usage(ROOT)
        disk = {"free_gb": round(du.free / 1e9, 1), "total_gb": round(du.total / 1e9, 1)}
    except OSError:
        disk = None
    host = {}
    try:
        mi = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition(":")
                mi[k] = int(v.split()[0]) * 1024
        host = {"used_gb": round((mi["MemTotal"] - mi["MemAvailable"]) / 2**30, 1),
                "total_gb": round(mi["MemTotal"] / 2**30, 1),
                "swap_gb": round((mi["SwapTotal"] - mi["SwapFree"]) / 2**30, 1)}
    except Exception:                                       # noqa: BLE001
        pass
    return {"source": src, "gpus": gpus, "disk": disk, "host": host}


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _project_slugs():
    if not os.path.isdir(PROJECTS):
        return []
    return sorted(d for d in os.listdir(PROJECTS)
                  if os.path.isfile(os.path.join(PROJECTS, d, "blueprint.json")))


def _count(d, exts):
    if not os.path.isdir(d):
        return 0
    return len([f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in exts])


def projects_index():
    out = []
    for slug in _project_slugs():
        base = os.path.join(PROJECTS, slug)
        bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
        lib = _read_json(os.path.join(base, "assets", "prompts.json"), {}) or {}
        rel = _read_json(os.path.join(base, "relations.json"), {}) or {}
        n_clip = 0
        clip_dir = os.path.join(base, "clips")
        if os.path.isdir(clip_dir):
            for _dp, _dn, files in os.walk(clip_dir):
                n_clip += len([f for f in files if f.lower().endswith((".mp4", ".webm"))])
        out.append({
            "slug": slug, "title": bp.get("title", ""), "logline": bp.get("logline", ""),
            "revision": bp.get("revision", 1),
            "chapters_planned": len(bp.get("chapters", [])),
            "chapters_written": _count(os.path.join(base, "chapters"), {".json"}),
            "scripts": _count(os.path.join(base, "script"), {".json"}),
            "prompts": len(lib.get("items", [])),
            # Both of these are easy to forget to run and invisible once you
            # leave the page that makes them, so the hub counts them.
            "relations": len(rel.get("edges", []) or []),
            "factions": len(rel.get("factions", []) or []),
            "images": _count(os.path.join(base, "assets", "images"), MEDIA_EXT),
            "clips": n_clip,
        })
    return {"projects": out}


KIND_LABEL = {"character": "人物", "location": "场景", "prop": "道具"}


def chapter_text(slug, n):
    """Full prose plus the director's script for one chapter.

    Served from web_server rather than story_server so the text stays readable
    after step 1 is shut down to free VRAM -- reading what you already made
    should never require the LLM to be loaded."""
    base = os.path.join(PROJECTS, slug)
    if not os.path.isfile(os.path.join(base, "blueprint.json")):
        return None
    ch = _read_json(os.path.join(base, "chapters", "%02d.json" % n))
    sc = _read_json(os.path.join(base, "script", "%02d.json" % n))
    if ch is None and sc is None:
        return None
    return {"n": n, "chapter": ch or {}, "script": sc or {}}


def blueprint_of(slug):
    return _read_json(os.path.join(PROJECTS, slug, "blueprint.json"))


def library(slug):
    """Everything generated for one project, grouped and labelled.

    Assets group by kind and carry the blueprint id plus the human name, so a
    plate reads "lo01 沈家小院 · 夜" rather than a filename. Clips group by
    chapter and stay keyed to their shot id, so one that has not been rendered
    shows as a labelled gap instead of quietly not being there.
    """
    base = os.path.join(PROJECTS, slug)
    if not os.path.isfile(os.path.join(base, "blueprint.json")):
        return None
    bp = _read_json(os.path.join(base, "blueprint.json"), {}) or {}
    lib = _read_json(os.path.join(base, "assets", "prompts.json"), {}) or {}
    names = {}
    for key in ("characters", "locations", "props"):
        for row in bp.get(key, []) or []:
            names[row.get("id")] = row.get("name", "")

    seen = {}
    for item in lib.get("items", []) or []:
        rel = "assets/images/" + (item.get("file") or "")
        full = os.path.join(base, *rel.split("/"))
        entry = dict(item)
        entry["name"] = names.get(item.get("id"), "") or item.get("label", "")
        if item.get("view"):
            entry["name"] = "%s · %s" % (entry["name"], item["view"])
        entry["exists"] = os.path.isfile(full)
        if entry["exists"]:
            _take_adopt(full)      # a pre-takes render shows up as take 1
        # ?t= is the modification time: a re-render changes the bytes behind
        # the same path, and the browser must not keep showing the old ones.
        entry["url"] = ("/media/%s/%s?t=%d" % (slug, rel, int(os.path.getmtime(full)))) if entry["exists"] else None
        entry["bytes"] = os.path.getsize(full) if entry["exists"] else 0
        entry["takes"] = _takes_for(slug, rel, full) if entry["exists"] else []
        entry["take"] = _take_active(full) if entry["exists"] else None
        # the two halves the 提示词 editor shows and edits, as actually sent
        entry["prompt_eff"] = sheet_prompt(item)
        entry["negative_eff"] = sheet_negative(item)
        entry["negative_read"] = sheet_cfg(item) > 1.0
        entry["prompt_edited"] = bool(str(item.get("prompt_user") or "").strip())
        entry["negative_edited"] = item.get("negative_user") is not None
        seen.setdefault(item.get("kind", "other"), []).append(entry)

    groups = []
    for kind in ("character", "location", "prop"):
        items = seen.get(kind)
        if items:
            groups.append({"kind": kind, "label": KIND_LABEL.get(kind, kind),
                           "items": items,
                           "done": sum(1 for i in items if i["exists"]),
                           "total": len(items)})

    chapters = []
    for c in bp.get("chapters", []) or []:
        n = c.get("n")
        sc = _read_json(os.path.join(base, "script", "%02d.json" % n), {}) or {}
        ch = _read_json(os.path.join(base, "chapters", "%02d.json" % n), {}) or {}
        shots = []
        for sh in sc.get("shots", []) or []:
            sid = sh.get("id", "")
            rel = "clips/%02d/%s.mp4" % (n, sid)
            full = os.path.join(base, *rel.split("/"))
            ok = os.path.isfile(full)
            if ok:
                _take_adopt(full)          # a clip made before takes existed becomes take 1
            cam = sh.get("camera", {}) or {}
            shots.append({
                "id": sid, "seconds": sh.get("seconds"),
                "slug_line": sh.get("slug_line", ""),
                "location_id": sh.get("location_id", ""),
                "location_name": names.get(sh.get("location_id"), ""),
                "characters": [{"id": i, "name": names.get(i, "")}
                               for i in (sh.get("characters") or [])],
                "shot_size": cam.get("shot_size", ""), "movement": cam.get("movement", ""),
                "video_prompt": sh.get("video_prompt", ""),
                "lines": len(sh.get("dialog") or []),
                # Step 3 needs both: the reference image this shot starts from,
                # and the line to be spoken over it.
                "first_frame_ref": sh.get("first_frame_ref", ""),
                # The assembly sheet needs these to know which joins the script
                # asked to dissolve rather than cut.
                "transitions": sh.get("transitions") or {},
                "narration": sh.get("narration", "") or "",
                "dialog": [{"who": d.get("who", ""), "line": d.get("line", "")}
                           for d in (sh.get("dialog") or [])],
                # How much GPU this shot IS, in the only unit that is
                # constant: segments. The rendered clip's own count when it
                # has one, the same estimate _render will use when it does
                # not (the narration sets the length, not `seconds`).
                "segs": int(_take_meta(full).get("segments") or 0) or _shot_segments(sh),
                "exists": ok, "bytes": os.path.getsize(full) if ok else 0,
                "clip_seconds": _clip_seconds(full) if ok else 0.0,
                "url": ("/media/%s/%s?t=%d" % (slug, rel, int(os.path.getmtime(full)))) if ok else None,
                "takes": _takes_for(slug, rel, full) if ok else [],
                "take": _take_active(full) if ok else None,
            })
        chapters.append({"n": n, "title": c.get("title", ""),
                         "summary": ch.get("summary", "") or c.get("summary", ""),
                         "words": len(ch.get("prose", "") or ""),
                         "shots": shots,
                         "done": sum(1 for x in shots if x["exists"]), "total": len(shots)})

    # The relation graph is generated in step 1 and then invisible everywhere
    # else; the library is where you go to look at what a project holds, so it
    # belongs here with names resolved rather than raw ids.
    rel = _read_json(os.path.join(base, "relations.json"), {}) or {}
    REL_LABEL = {"kin": "血缘", "ally": "盟友", "enemy": "敌对",
                 "mentor": "师徒", "romance": "情感", "duty": "职责"}
    # Nodes carry their own ids and faction so the console can lay out a graph;
    # the pair strings below stay for the plain list.
    fac_of = {}
    for i, f in enumerate(rel.get("factions") or []):
        for m in (f.get("members") or []):
            fac_of[m] = i
    nodes = [{"id": c.get("id"), "name": c.get("name", "") or c.get("id"),
              "role": c.get("role", ""), "tier": c.get("tier", ""),
              "faction": fac_of.get(c.get("id"), -1)}
             for c in (bp.get("characters") or [])] if rel else []
    relations = {
        "nodes": nodes,
        "factions": [{"name": f.get("name", ""), "stance": f.get("stance", ""),
                      "palette": f.get("palette", ""), "insignia": f.get("insignia", ""),
                      "members": [names.get(m, m) for m in (f.get("members") or [])]}
                     for f in (rel.get("factions") or [])],
        "edges": [{"kind": REL_LABEL.get(e.get("type"), e.get("type", "")),
                   "type": e.get("type", ""),
                   "from": e.get("from", ""), "to": e.get("to", ""),
                   "strength": e.get("strength") or 1,
                   "pair": "%s — %s" % (names.get(e.get("from"), e.get("from", "")),
                                        names.get(e.get("to"), e.get("to", ""))),
                   "subtype": e.get("subtype", ""), "label": e.get("label", ""),
                   "shared": e.get("shared_features", ""),
                   "turns": ["第%s章转为%s" % (t.get("chapter"),
                                             REL_LABEL.get(t.get("becomes"), t.get("becomes", "")))
                             for t in (e.get("turns") or [])]}
                  for e in (rel.get("edges") or [])],
    }
    return {"slug": slug, "title": bp.get("title", ""), "logline": bp.get("logline", ""),
            "aspect": (bp.get("visual_style", {}) or {}).get("aspect", "9:16"),
            "groups": groups, "chapters": chapters, "relations": relations}


STEPS = [
    ("1", "写小说", "Story Studio.dc.html",
     "故事基础与结构 -> 制作圣经 -> 逐章正文 -> 导演分镜（机位/光比/轴线/台词念法）",
     "只用档位 A · 大模型", 8010),
    ("2", "出参考图", "Reference Images.dc.html",
     "按第一步的提示词出人物白板图、场景空景、道具图，入库并打标签",
     "只用档位 B · ComfyUI Flux.2", 7860),
    ("3", "出片", "Pipeline Runner.dc.html",
     "按分镜逐镜出 10 秒片段，按章拼成成片",
     "只用档位 C · MiniMax-H3", 9000),
]


def _port_up(port, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


# The hub's 载入小说 block. Plain HTML + a little script, like the rest of the
# hub: it has to work when the design system or React is not there.
_LOADER_HTML = r"""
<h3>载入小说</h3>
<div class=card><div class=body>
<p>选一个 .txt（UTF-8 / GBK / Big5）。系统会自动建项目，按原书的「第X章 / 第X回」标题分章，每章再切成约 1500 字一段
（一段 = 一次分镜 = 一部分段成片），然后依次跑：摘要与蓝图 → 人物关系 → 逐段分镜 → 资产提示词 → 出参考图 → 按章出片，
每章的段都出齐后再拼成原书一章的成片。全程在服务端跑，关掉页面不影响；中途重启会从断点续上。</p>
<div class=lform>
<label>小说文件<input type=file id=nv-file accept=".txt,text/plain"></label>
<label>书名<input id=nv-title placeholder="例如：天龙八部"></label>
<label>时代（可选，填了就以此为准）<input id=nv-era placeholder="例如：北宋"></label>
<label>影像风格（可选）<input id=nv-style placeholder="例如：写实电影感，冷暖对比"></label>
<label>每段字数<input id=nv-part type=number value=1500 min=600 max=4000 step=100></label>
</div>
<div class=lbtn><button id=nv-preview>预览分章</button><button id=nv-go class=pri>载入并开始生成</button>
<span id=nv-msg class=lmsg></span></div>
<div id=nv-books class=lbooks></div>
</div></div>
<div id=nv-runs></div>
<script>
(function(){
  const $ = id => document.getElementById(id);
  const esc = t => String(t == null ? "" : t).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  function file() { const f = $("nv-file").files[0]; if (!f) throw new Error("先选一个小说文件"); return f; }
  function qs(f) {
    const t = $("nv-title").value.trim() || f.name.replace(/\.[^.]+$/, "");
    return "?title=" + encodeURIComponent(t) + "&era=" + encodeURIComponent($("nv-era").value.trim()) +
      "&style=" + encodeURIComponent($("nv-style").value.trim()) + "&part_chars=" + encodeURIComponent($("nv-part").value);
  }
  async function send(path) {
    const f = file();
    $("nv-msg").textContent = "上传中…";
    const r = await fetch(path + qs(f), { method: "POST", body: await f.arrayBuffer() });
    const j = await r.json().catch(() => ({}));
    if (!j.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }
  function showBooks(j) {
    const b = j.books || [];
    $("nv-books").innerHTML = "<div class=lsum>共 " + (j.chars || 0) + " 字 · 原书 " + b.length + " 章 → " +
      (j.parts || 0) + " 段。每段的分镜约几分钟，出片每段约数小时 GPU。</div>" +
      b.slice(0, 200).map(x => "<div class=lrow><span>" + esc(x.title || ("第" + x.n + "章")) +
        "</span><span>" + x.parts.length + " 段</span></div>").join("") +
      (b.length > 200 ? "<div class=lrow>…</div>" : "");
  }
  $("nv-preview").onclick = async () => {
    try { const j = await send("/api/autorun/preview"); showBooks(j); $("nv-msg").textContent = ""; }
    catch (e) { $("nv-msg").textContent = "× " + e.message; }
  };
  $("nv-go").onclick = async () => {
    try {
      const f = file();
      if (!confirm("开始为《" + ($("nv-title").value.trim() || f.name) + "》自动生成？\n写剧本时会停掉 ComfyUI，出图出片会占用 GPU 很长时间。")) return;
      const j = await send("/api/autorun/load");
      $("nv-msg").textContent = "已开始：项目 " + j.slug; poll();
    } catch (e) { $("nv-msg").textContent = "× " + e.message; }
  };
  async function act(slug, what) {
    await fetch("/api/autorun/" + what, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slug }) });
    poll();
  }
  window.nvAct = act;
  const STATUS = { running: "运行中", done: "已完成", incomplete: "未出齐", failed: "失败", stopped: "已停止", idle: "未开始" };
  async function poll() {
    try {
      const j = await (await fetch("/api/autorun")).json();
      const runs = j.runs || [];
      $("nv-runs").innerHTML = runs.length ? "<h3>自动流程</h3>" + runs.map(r => {
        const keys = (r.stages || []).map(s => s.key);
        const at = keys.indexOf(r.stage);
        const pct = r.status === "done" ? 100 : Math.max(0, Math.round(at / Math.max(1, keys.length) * 100));
        const log = (r.log || []).slice().reverse().map(l =>
          "<div>" + new Date(l.t * 1000).toLocaleString() + "　" + esc(l.msg) + "</div>").join("");
        const btn = r.running
          ? "<button onclick=\"nvAct('" + esc(r.slug) + "','stop')\">停下</button>"
          : (r.status !== "done" ? "<button onclick=\"nvAct('" + esc(r.slug) + "','start')\">继续</button>" : "");
        return "<div class=card><div class=body><h2><a href='/Library.dc.html?slug=" + encodeURIComponent(r.slug) + "'>" +
          esc((r.opts && r.opts.title) || r.slug) + "</a> <span class=lst>" + (STATUS[r.status] || r.status) +
          (r.stop_requested && r.running ? "（停止中）" : "") + "</span></h2>" +
          "<div class=meta>" + esc(r.stage_label || "") + (r.error ? "　× " + esc(r.error) : "") +
          "<span class=slot><a href='/Pipeline%20Runner.dc.html?slug=" + encodeURIComponent(r.slug) + "'>看出片</a>　" + btn + "</span></div>" +
          "<div class=vbar><div style='width:" + pct + "%'></div></div>" +
          ((r.films || []).length ? "<div class=lfilms>成片：" + r.films.map(f =>
            "<a target=_blank href='" + f.url + "'>" + esc(f.title || ("第" + f.book + "章")) + "</a>").join("　") + "</div>" : "") +
          "<div class=llog>" + log + "</div></div></div>";
      }).join("") : "";
      if (runs.some(r => r.running)) setTimeout(poll, 10000);
    } catch (e) { setTimeout(poll, 30000); }
  }
  poll();
})();
</script>
"""

_LOADER_CSS = (
    ".lform{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px 14px;margin:10px 0}"
    ".lform label{display:flex;flex-direction:column;font-size:12px;color:var(--mut);gap:4px}"
    ".lform input{font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--line);border-radius:6px;"
    "background:var(--bg);color:var(--fg)}"
    ".lbtn{display:flex;gap:10px;align-items:center;flex-wrap:wrap}"
    "button{font:inherit;font-size:13px;padding:6px 14px;border:1px solid var(--line);border-radius:6px;"
    "background:var(--card);color:var(--fg);cursor:pointer}button.pri{background:var(--acc);color:#fff;border-color:var(--acc)}"
    ".lmsg{font-size:12.5px;color:var(--mut)}.lbooks{margin-top:10px;font-size:12.5px;max-height:260px;overflow:auto}"
    ".lsum{color:var(--mut);margin-bottom:6px}.lrow{display:flex;justify-content:space-between;"
    "border-bottom:1px solid var(--line);padding:3px 0}.lst{font-size:12px;color:var(--mut);font-weight:400}"
    ".lfilms{font-size:12.5px;margin-top:6px}"
    ".llog{font-size:11.5px;color:var(--mut);margin-top:6px;max-height:120px;overflow:auto;line-height:1.6}"
)


def _hub_html():
    pages = set(_pages())
    cards = []
    for num, name, page, desc, slot, port in STEPS:
        up = _port_up(port)
        href = ("/" + page.replace(" ", "%20")) if (page and page in pages) else None
        dot = "on" if up else "off"
        title = ('<a href="%s">%s</a>' % (href, name)) if href else name
        cards.append(
            '<div class=card><div class=num>%s</div>'
            '<div class=body><h2>%s</h2><p>%s</p>'
            '<div class=meta><span class="dot %s"></span>:%d %s<span class=slot>%s</span></div>'
            '</div></div>' % (num, title, desc, dot, port,
                              "在线" if up else "未启动", slot))
    proj = projects_index()["projects"]
    if proj:
        rows = "".join(
            '<tr><td><a href="/Library.dc.html?slug=%s">%s</a><span class=sub>%s</span></td>'
            '<td>%d/%d 章</td><td>%d 分镜</td><td>%s</td><td>%s</td><td>%d 图</td><td>%d 片段</td></tr>'
            % (p["slug"], p["title"] or p["slug"], p["logline"][:40],
               p["chapters_written"], p["chapters_planned"], p["scripts"],
               # "未生成" reads as a state; "0 条" reads like a failure.
               ("%d 条 / %d 派系" % (p["relations"], p["factions"]))
               if p["relations"] or p["factions"] else "未生成",
               ("%d 条" % p["prompts"]) if p["prompts"] else "未生成",
               p["images"], p["clips"])
            for p in proj)
        table = ("<h3>项目</h3><table><tr><th>作品<th>正文<th>分镜<th>关系图<th>提示词<th>参考图<th>片段</tr>"
                 + rows + "</table>")
    else:
        table = ("<h3>项目</h3><p class=empty>还没有项目。从第一步开始：填故事基础与结构，"
                 "生成制作圣经。</p>")
    other = "".join('<a href="/%s">%s</a>' % (n.replace(" ", "%20"), n)
                    for n in sorted(pages) if n not in ("Story Studio.dc.html",
                                                        "Pipeline Runner.dc.html"))
    gi = gpu_info()
    g0 = (gi.get("gpus") or [None])[0]
    if g0 and g0.get("mem_total_gb"):
        pct = int(round((g0.get("mem_used_gb") or 0) / g0["mem_total_gb"] * 100))
        vram = ('<div class=vram><span>%s</span>'
                '<div class=vbar><div style="width:%d%%"></div></div>'
                '<span class=vnum>%s / %s GB</span></div>'
                % (g0.get("name", "GPU"), pct,
                   g0.get("mem_used_gb"), g0["mem_total_gb"]))
    else:
        vram = '<div class=vram><span>显存读数不可用</span></div>'
    return (
        "<!doctype html><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>书影 Bookreel</title><style>"
        ":root{--bg:#faf8f5;--fg:#241f1a;--mut:#6d635a;--line:#e2dcd3;--acc:#8a5a2b;--card:#fff}"
        "@media(prefers-color-scheme:dark){:root{--bg:#14120f;--fg:#e8e2d9;--mut:#9c948a;--line:#2e2a25;--acc:#c89b6a;--card:#1c1916}}"
        "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);"
        "font:15px/1.7 system-ui,-apple-system,'Noto Sans SC',sans-serif}"
        ".wrap{max-width:960px;margin:0 auto;padding:36px 22px 60px}"
        "h1{font-size:23px;margin:0 0 4px}.lead{color:var(--mut);margin:0 0 28px;font-size:13.5px}"
        "h3{font-size:14px;letter-spacing:.12em;color:var(--acc);margin:34px 0 12px;font-weight:600}"
        ".card{display:flex;gap:16px;background:var(--card);border:1px solid var(--line);"
        "border-radius:10px;padding:16px 18px;margin-bottom:12px}"
        ".num{font-size:26px;color:var(--acc);font-weight:600;min-width:26px;line-height:1.2}"
        ".body{flex:1;min-width:0}h2{font-size:16px;margin:0 0 4px}"
        ".card p{margin:0 0 8px;color:var(--mut);font-size:13px}"
        ".meta{font-size:12px;color:var(--mut);display:flex;align-items:center;gap:7px}"
        ".slot{margin-left:auto;opacity:.75}"
        ".dot{width:7px;height:7px;border-radius:50%;display:inline-block}"
        ".dot.on{background:#3f9142}.dot.off{background:#b8b0a6}"
        "table{width:100%;border-collapse:collapse;font-size:13px}"
        "th{text-align:left;font-weight:500;color:var(--mut);font-size:11.5px;"
        "letter-spacing:.06em;border-bottom:1px solid var(--line);padding:6px 8px 6px 0}"
        "td{padding:9px 8px 9px 0;border-bottom:1px solid var(--line);vertical-align:top}"
        ".sub{display:block;color:var(--mut);font-size:11.5px}"
        "a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}"
        ".empty{color:var(--mut);font-size:13px}"
        ".more{margin-top:26px;font-size:12.5px;color:var(--mut)}.more a{margin-right:14px}"
        ".vram{position:absolute;top:34px;right:22px;text-align:right;font-size:11.5px;"
        "color:var(--mut);min-width:210px;white-space:nowrap}"
        ".vbar{height:4px;background:var(--line);border-radius:2px;overflow:hidden;margin:6px 0 4px}"
        ".vbar div{height:100%;background:var(--acc)}"
        ".vnum{font-variant-numeric:tabular-nums}"
        + _LOADER_CSS +
        "</style>"
        "<div class=wrap style='position:relative'>" + vram + "<h1>书影 Bookreel</h1>"
        "<p class=lead>小说 → 参考图 → 成片。三步各自独占 GPU，跑完一步关掉它再起下一步，"
        "显存才够用。</p>"
        + _LOADER_HTML
        + "".join(cards) + table
        + ("<div class=more>" + other + "</div>" if other else "")
        + "</div>"
    )


def _lan_ip():
    """Best-effort LAN address, for printing a URL the phone can actually use.
    No packet is sent -- connect() on UDP just picks the outbound interface."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))     # TEST-NET-1, never routed
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    global COMFY
    ap = argparse.ArgumentParser(description="Serve the Bookreel web console")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--comfy", default=COMFY, help="ComfyUI base URL the render queue submits to")
    args = ap.parse_args()
    COMFY = args.comfy.rstrip("/")
    WEB_PORT[0] = args.port

    pages = _pages()
    # A batch that was running when this process last died carries on now.
    for slug in _project_slugs():
        try:
            if batch_for(slug).resume():
                print("[web] resumed render batch for '%s'" % slug, flush=True)
            if video_batch_for(slug).resume():
                print("[web] resumed video batch for '%s'" % slug, flush=True)
            if lipsync_batch_for(slug).resume():
                print("[lipsync] resumed %s" % slug, flush=True)
            if upscale_batch_for(slug).resume():
                print("[web] resumed 4K batch for '%s'" % slug, flush=True)
            if music_batch_for(slug).resume():
                print("[web] resumed music batch for '%s'" % slug, flush=True)
            if sfx_batch_for(slug).resume():
                print("[web] resumed sfx batch for '%s'" % slug, flush=True)
            if os.path.isfile(os.path.join(PROJECTS, slug, "autorun.json")) \
                    and autorun_for(slug).resume():
                print("[web] resumed novel autorun for '%s'" % slug, flush=True)
        except Exception as exc:                            # noqa: BLE001
            print("[web] ! could not resume batch for '%s': %s" % (slug, exc), flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    print("[web] serving  %s" % ROOT, flush=True)
    print("[web] listening: http://%s:%d" % (args.host, args.port), flush=True)
    if pages:
        print("[web] console  : http://%s:%d/%s"
              % (args.host, args.port, pages[0].replace(" ", "%20")), flush=True)
    if args.host in ("0.0.0.0", "::"):
        ip = _lan_ip()
        if ip and pages:
            print("[web] from other devices on this network:", flush=True)
            print("[web]   http://%s:%d/%s"
                  % (ip, args.port, pages[0].replace(" ", "%20")), flush=True)
        print("[web] NOTE: anyone on this network can open the console. The "
              "console only shows the UI unless the model services are also "
              "bound to the LAN (BIND_LAN=1).", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
