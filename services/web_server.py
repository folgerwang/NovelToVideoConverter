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
import io
import json
import os
import posixpath
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import random
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request
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
VERIFY_RETRIES = 2
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
        g = (c.get("gender") or "").strip()
        if g in ("男", "女"):
            qs.append(("性别", "人物是男是女（男/女）", lambda v: None if g in str(v) else "性别看起来是 %s，应为 %s" % (v, g), "人物是%s性" % g))
        if view in ("左侧", "右侧"):
            want = "左" if view == "左侧" else "右"
            qs.append(("面朝", "人物的脸朝向画面的左边缘还是右边缘（左/右）", lambda v: None if want in str(v) else "面朝%s，应面朝%s" % (v, want), "人物面朝画面%s侧" % want))
        if view == "背面":
            qs.append(("看得到脸", "能看到人物的脸吗（是/否）", lambda v: "看得到脸，应背对镜头" if yes(v) else None, "人物背对镜头，看不到脸"))
        text = " ".join(str(c.get(k, "")) for k in ("costume", "signature_prop"))
        if "刀" in text or "剑" in text:
            qs.append(("刀的数量", "画面里一共有几把刀或剑，鞘中的和手里拿着的分开数、都算（数字）", lambda v: None if num(v) in (1,) else "刀有 %s 把，应只有一把" % v, "双手空着、自然下垂，不拿任何东西；唯一的一把刀完整插在腰间的刀鞘里，刀刃看不见，只露刀柄"))
            qs.append(("刀鞘数量", "刀鞘有几个（数字）", lambda v: None if (num(v) or 0) <= 1 else "刀鞘 %s 个" % v, ""))
            qs.append(("出鞘刀身数量", "出鞘露出的刀身有几条（数字）", lambda v: None if (num(v) or 0) == 0 else "有 %s 条出鞘刀身，刀应在鞘中" % v, "双手空着自然下垂，刀完整在鞘中挂在腰间，刀刃看不见"))
        qs.append(("现代物品", "有没有现代物品：系带靴、拉链、手表、眼镜、塑料、印刷品等（有/无，有的话写出是什么）", lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "现代物品：%s" % v, "没有任何现代物品，鞋是该时代的靴子，没有鞋带"))
        qs.append(("鞋带", "鞋或靴子上有没有鞋带（有/无）", lambda v: "靴子有鞋带（现代系带靴）" if str(v).strip() in ("有", "yes", "是") else None, "脚上是明代铁底皮靴：整片皮面、靴筒到小腿、无鞋带无绑带"))
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
    elif kind == "prop":
        # a brush with its hanging strap, a sword with its scabbard: the accessory
        # the description names is part of the object, not a second item
        qs.append(("物件数量", "不算描述里提到的配件（笔挂布条、刀鞘、系绳、书签之类），画面里有几件独立的物品（数字）", lambda v: None if num(v) == 1 else "物件 %s 件，应只有一件" % v, "只有一件物品及其自身的配件"))
        # the brush came back with a head at each end; ask about duplicated parts
        qs.append(("重复部件", "这件物品有没有重复或多出来的部件，例如毛笔两端都有笔头、刀有两个刀柄、书有两个书脊（有/无，有的话写出是什么）",
                   lambda v: None if str(v).strip() in ("无", "没有", "no", "none", "否", "") else "重复部件：%s" % v, "物件结构正确，每个部件只有一个"))
        name = str(item.get("label") or "")
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
                have = set(self.state["queue"]) | {self.state.get("current") or ""}
                fresh = [k for k in keys if k not in have]
                if front:
                    self.state["queue"][0:0] = fresh
                else:
                    self.state["queue"].extend(fresh)
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
            self._log("服务重启，续跑：剩余 %d 张" % len(st["queue"]))
            self.stop_flag = False
            self.thread = threading.Thread(target=self._run, name="batch-" + self.slug, daemon=True)
            self.thread.start()
            return True
        return False

    def stop(self):
        self.stop_flag = True
        self._log("收到停止：当前这张出完就停")

    def skip(self, keys):
        """Drop waiting items (a picture accepted by hand does not need its re-render)."""
        with self.lock:
            before = len(self.state["queue"])
            self.state["queue"] = [k for k in self.state["queue"] if k not in set(keys)]
            n = before - len(self.state["queue"])
            cur = self.state.get("current")
            self._save()
        if cur in keys:
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
                extra = ""
                full = os.path.join(PROJECTS, self.slug, "assets", "images", item["file"])
                for attempt in range(1 + (VERIFY_RETRIES if VERIFY_ENABLED else 0)):
                    if mode == "check" and attempt == 0:
                        # re-check the picture already on disk (its verdict was skipped
                        # or is stale); only render if it fails
                        n = _take_active(full)
                        if not n or not os.path.isfile(full):
                            raise RuntimeError("没有可核对的图")
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
                        extra = "【必须】" + verdict["fix"] + "。"
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
        wf["20"]["inputs"]["text"] = ((extra + " ") if extra else "") + item["prompt"]
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
VIDEO_SECONDS = float(os.environ.get("BOOKREEL_CLIP_SECONDS", "15.0"))
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
    if "俯" in (cam.get("angle") or "") or "过顶" in (cam.get("height") or ""):
        want = ["_top"] + side
    else:
        m = re.search(r"S(\d+)$", shot.get("id") or "")
        k = int(m.group(1)) % len(side) if m else 0
        want = side[k:] + side[:k] + ["_top"]
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
            return "/media/%s/assets/images/%s" % (slug, urllib.parse.quote(it["file"])), note
    return None, "no plate for %s" % loc


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
VOICE = os.environ.get("BOOKREEL_VOICE", "storyteller")
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


def _speech_lines(shot):
    """Everything heard in this shot, in order: the narrator, then the dialog.

    CosyVoice2 is zero-shot and has no speaker table, so a character speaks in
    their own voice only once somebody records voices/<id>.wav; otherwise the
    narrator reads their line too. That is a casting decision, not a failure,
    so it is not an error - but /api/video/plan reports which ids are missing.
    """
    out = []
    nar = (shot.get("narration") or "").strip()
    if nar:
        out.append((VOICE, nar))
    for d in shot.get("dialog") or []:
        line = (d.get("line") or "").strip()
        if not line:
            continue
        who = (d.get("who") or "").strip()
        have = who and os.path.isfile(os.path.join(ROOT, "voices", who + ".wav"))
        out.append((who if have else VOICE, line))
    return out


def _synth(text, voice, timeout=1800):
    req = urllib.request.Request(
        TTS + "/tts",
        data=json.dumps({"text": text, "voice": voice, "speed": 0.95,
                         "format": "wav"}).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


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
    pc, cc = prev.get("camera") or {}, cur.get("camera") or {}
    if (pc.get("angle") or "") != (cc.get("angle") or ""):
        return None
    a = _SHOT_SCALE.get(pc.get("shot_size") or "")
    b = _SHOT_SCALE.get(cc.get("shot_size") or "")
    if a is None or b is None:
        return None
    if b > a + 1e-9:
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

    def start(self, keys, front=False, fmt=None):
        with self.lock:
            if self.thread and self.thread.is_alive():
                have = set(self.state["queue"]) | {self.state.get("current") or ""}
                fresh = [k for k in keys if k not in have]
                if front:
                    self.state["queue"][0:0] = fresh
                else:
                    self.state["queue"].extend(fresh)
                self._save()
                return {"ok": True, "appended": True, "queued": len(self.state["queue"]),
                        "format": self.state.get("format")}
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
                self.state["current"] = key
                self.state["current_started"] = int(time.time())
                self._save()
            try:
                self._render(key)
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
                    self._save()

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
            try:
                with urllib.request.urlopen(
                        H3 + "/v1/query/video_generation?task_id=" + tid, timeout=60) as r:
                    q = json.load(r)
            except Exception:                               # noqa: BLE001
                continue
            st = q.get("status")
            if st == "Fail":
                raise RuntimeError(str(q.get("error"))[:200])
            if st != "Success":
                if time.time() - t0 > 6 * 3600:
                    raise RuntimeError("%s 等了 6 小时没有结果" % label)
                continue
            fid = q.get("file_id")
            if not fid:
                raise RuntimeError("%s 完成但没有文件" % label)
            with urllib.request.urlopen(H3 + "/v1/files/" + fid, timeout=600) as r:
                return r.read(), q

    def _voice_track(self, work, key, shot):
        """Synthesize everything heard in this shot into one wav.

        Returns (path, seconds); (None, 0.0) when there is nothing to say or
        the tts service is not up. A silent shot is a normal outcome - plenty
        of shots are pure action - so a missing narrator is logged, not raised:
        losing the whole 56-minute render because :9100 was down would be a
        poor trade.
        """
        lines = _speech_lines(shot)
        if not lines:
            return None, 0.0
        parts = []
        for i, (voice, text) in enumerate(lines):
            wav = os.path.join(work, "%s-voice-%d.wav" % (shot["id"], i))
            try:
                data = _synth(text, voice)
            except Exception as exc:                        # noqa: BLE001
                self._log("！%s 旁白/台词未出（%s）：%s" % (key, voice, str(exc)[:90]))
                return None, 0.0
            with open(wav, "wb") as f:
                f.write(data)
            parts.append(wav)
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
        return out, _duration(out)

    def _render(self, key):
        n, shot = self._shot(key)
        if not shot:
            raise RuntimeError("script 里没有 %s" % key)
        prompt = (shot.get("video_prompt") or shot.get("slug_line") or "").strip()
        if not prompt:
            raise RuntimeError("这一镜没有 video_prompt")
        lib = _read_json(os.path.join(PROJECTS, self.slug, "assets", "prompts.json"), {}) or {}
        base = os.path.join(PROJECTS, self.slug)
        work = os.path.join(base, "clips", "%02d" % n, "_frames")
        os.makedirs(work, exist_ok=True)
        t0 = time.time()

        # 1. What has to be heard decides how long the picture must be. The
        #    script asks for 10 s a shot and the narration for these runs 10-13
        #    s, so a 4 s clip was never going to carry its own line.
        voice, voice_secs = self._voice_track(work, key, shot)
        seg = self._grid_seconds(SEG_SECONDS)
        target = max(VIDEO_MIN_SECONDS, VIDEO_SECONDS, voice_secs)
        nseg = 1
        while nseg * seg < target - 1e-6:
            nseg += 1

        # 2. Where the first segment starts. Inheriting the previous shot's
        #    last frame is what stops 108 clips from each opening on the same
        #    location plate, which is what made them look isolated.
        prev = getattr(self, "_prev", None)
        ref, note = None, ""
        ratio = _inherit_ratio(prev["shot"], shot) if (
            prev and _adjacent(prev["key"], key) and os.path.isfile(prev["png"])) else None
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
            ref, note = _pick_first_frame(self.slug, shot, lib)

        body = {"model": "MiniMax-Hailuo-02", "prompt": prompt,
                "duration": SEG_SECONDS,
                "resolution": VIDEO_FORMATS.get(self.state.get("format"), VIDEO_RESOLUTION),
                "prompt_optimizer": False}

        self._log("▶ %s %.2f 秒 = %d 段 x %.2f，旁白 %.1f 秒，首帧 %s"
                  % (key, nseg * seg, nseg, seg, voice_secs, note))

        # 3. Render the segments, each one starting where the last one ended.
        parts, canvas, tail = [], "", None
        for i in range(nseg):
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
            self._log("   %s %d/%d 段完成（%.0f 分）" % (key, i + 1, nseg,
                                                        (time.time() - t0) / 60.0))

        # 4. Join them, then lay the Chinese over h3's invented audio.
        joined = os.path.join(work, "%s-joined.mp4" % shot["id"])
        _concat_stream(parts, joined)
        final = os.path.join(work, "%s-final.mp4" % shot["id"])
        if voice and os.path.isfile(voice):
            if AMBIENT_GAIN > 0 and _has_audio(joined):
                _ff(["-i", joined, "-i", voice, "-filter_complex",
                     "[0:a]volume=%.3f[amb];[amb][1:a]amix=inputs=2:duration=first:"
                     "dropout_transition=0,dynaudnorm=p=0.9[a]" % AMBIENT_GAIN,
                     "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                     "-b:a", "160k", final])
            else:
                # Narration only: the video's own track, if it has one, is
                # dropped rather than mixed. -shortest is deliberately absent -
                # the picture is sized to outlast the line, not the other way
                # round, and truncating the video to the narration would undo
                # that.
                _ff(["-i", joined, "-i", voice, "-map", "0:v", "-map", "1:a",
                     "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", final])
        elif _has_audio(joined):
            # Nothing said in this shot. h3's invented voice is worse than
            # silence, so strip it and leave the picture mute for the sound
            # pass; the script carries room_tone/ambience/foley per shot.
            _ff(["-i", joined, "-map", "0:v", "-c:v", "copy", "-an", final])
        else:
            final = joined

        with open(final, "rb") as f:
            data = f.read()
        rel = "clips/%02d/%s.mp4" % (n, shot["id"])
        full = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        secs = _duration(final)
        take = _take_add(full, data, {
            "seconds": round(secs, 2), "resolution": canvas or body["resolution"],
            "segments": nseg, "voice_seconds": round(voice_secs, 2),
            "first_frame": note, "elapsed": int(time.time() - t0)})
        self._log("✓ %s 第 %d 次，%.1f 秒，%d 段，%d 分，%.1f MB%s"
                  % (key, take, secs, nseg, (time.time() - t0) / 60.0, len(data) / 1e6,
                     "，有中文旁白" if voice else "，无台词"))

        # 5. Hand the tail to the next shot and clear the segment scratch.
        self._prev = {"key": key, "shot": shot, "png": tail} if tail else None
        for q in parts + [joined]:
            try:
                if q != final:
                    os.unlink(q)
            except OSError:
                pass
        return


_VIDEO = {}


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
            ref, note = _pick_first_frame(slug, sh, lib)
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
            return self._json({"ok": True, "skipped": b.skip(keys), "batch": b.snapshot()})
        return self._save()

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
        r = b.start(keys, mode, front=bool(req.get("front")))
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
        full = os.path.join(PROJECTS, *parts)
        if not os.path.realpath(full).startswith(os.path.realpath(PROJECTS) + os.sep):
            return self.send_error(404, "Not found")
        if not os.path.isfile(full):
            return self.send_error(404, "Not found")
        ext = os.path.splitext(full)[1].lower()
        try:
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(full, "rb") as fh:
                if not getattr(self, "_head_only", False):
                    shutil.copyfileobj(fh, self.wfile)
        except OSError:
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
                "narration": sh.get("narration", "") or "",
                "dialog": [{"who": d.get("who", ""), "line": d.get("line", "")}
                           for d in (sh.get("dialog") or [])],
                "exists": ok, "bytes": os.path.getsize(full) if ok else 0,
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
        "</style>"
        "<div class=wrap style='position:relative'>" + vram + "<h1>书影 Bookreel</h1>"
        "<p class=lead>小说 → 参考图 → 成片。三步各自独占 GPU，跑完一步关掉它再起下一步，"
        "显存才够用。</p>"
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
