# Bookreel — rules that must not regress

These were each earned by a wrong picture. They live in code; this file says
where, so nobody "simplifies" them away. Chinese labels are the ones the
console shows.

## Prompts (services/story_server.py)

- **Every image/video prompt carries the world in plain words, built in code.**
  `_anchor()` + `_anchored()` compose `head + model text + tail` for reference
  images (`/assets`) and shot `video_prompt`s (`/script`). The blueprint
  schema has `world.setting_type / era / place / geography / history / people`
  and `characters[].gender / ethnicity / social_class`; the LLM is required to
  fill them. Never rely on the LLM to "remember" era, ethnicity or gender —
  ch01 (a Ming male scholar) rendered as a European woman when we did.
- **Head is subject-first; culture/society go in the tail.** Opening tokens set
  the picture. Putting 运河/漕运/木帆船 in front made a portrait render as a
  fishing scene. `tech_level` (a list of objects) is only allowed behind
  locations and shots, never near a portrait or a single prop.
- **No negations for nouns you don't want.** A diffusion model hears the noun
  ("没有旗帜" → flags). Strip the word from the text instead and let the
  verifier catch it. `旗杆/旗帜` are banned from scene descriptions.
- **Characters: 4 passport-style views** (`CHAR_VIEWS`): 正面/左侧/右侧/背面,
  whole body, plain ground, one shared seed per character, left/right stated
  as screen direction in Chinese + English ("鼻尖指向画面右边缘 … facing
  right"), side/back views say the far side's features are not visible.
  Tail states prop counts ("一把刀就是一把刀，插在鞘中"), period footwear
  without laces, and that described scars are old, pale and healed — a
  "旧刀疤" otherwise renders as a fresh red gash, and a correction saying
  "没有伤口" contradicts the blueprint's own scar. The verifier asks about
  *fresh* wounds only. And never describe a scar with the character 刀
  ("刀疤从腕延伸至掌"): the model reads 刀 as a weapon cue — 陆横 got a
  second, drawn sword and a bleeding forearm from that one phrase. Write
  "旧伤痕", pale and healed. Likewise "腰佩长刀" draws a bare blade: name the
  sheath as the object ("腰间挂着一把带鞘的佩刀，刀刃完全收在黑漆木鞘里，
  只露出刀柄"), and on a reference sheet keep scars near-invisible ("很淡的
  白色旧痕") — the model over-paints any mark it is told about.
- **Scenes: 9 views** (`LOC_VIEWS`): 外·正/背/左/右, 内·正/背/左/右, 俯瞰,
  16:9, **one seed per view** (a shared seed gave 18 identical courtyards).
  The LLM describes a place in parts — `exterior` (front gate), `exterior_sides`
  (back/side walls, must not mention the gate), `layout` + `interior_front/
  back/left/right` (one wall each, consistent with the layout; the back view
  sees the door's inside and the *backs* of the main furniture), `roof`,
  `surroundings`, `light`, `style` — and each view is composed only from the
  parts its camera can see (`VIEW_PARTS`). Side-wall views say the main desk
  and chair are *not in frame* — "only at the frame's edge" put them back in
  the centre; the verifier asks whether the centre is a desk facing the
  camera. Side-wall views get their wall text only — the `layout` line names
  the desk, and the model centres whatever it is told about — and so does
  the room noun: "主事厅内朝西看" drew a centred desk three times with no desk
  in the text; side views say "室内朝西看". Raw parts are
  stored on each item
  (`parts`) so views can be re-composed without a model call.
- **Name the object, not the word.** "油灯" rendered as an electric desk lamp,
  "木格窗" got glass panes, "腰佩长刀" a bare blade, "旗杆" a steel flagpole.
  Scene text substitutes the object ("陶碟油灯（小陶碟盛油、一根灯芯明火、
  没有灯罩）") at composition time (`_OBJECTS`), and every window in any
  wording — 小窗（木棂）, 木棂窗, 花窗 — gets "木格糊纸、没有玻璃"
  (`_paper_windows`); the model glazes any window it is not told about.
  "书/书籍" on a shelf become modern spined paperbacks: scene text says
  "线装书（无书脊、平摊叠放、封面贴题签、纸页发黄）". Night light phrased as a
  shape becomes a fixture: "月光在地面形成冷蓝窄条" drew blue LED strips across
  the floor and "光锥" a wall spotlight — say "清冷的银白色月光", never a cone
  or a strip.
- **Props:** structure stated explicitly ("毛笔只有一端有笔头，挂绳系在没有
  笔头的杆尾") — the brush strap was drawn hanging from the ferrule; the
  verifier asks which end the strap is on.
- Location plates carry `time_of_day`; night plates get real night light,
  never the day prompt with "，夜" appended.
- **`/assets` never publishes fewer items than the project had.** The reply
  must cover every cast id and location id (`require=_complete`, 48k
  tokens); anything still missing is carried over from the previous
  prompts.json. A truncated reply once overwrote 97 items with 38 and the
  render queue failed 47 keys as "not in prompts.json".

## Rendering (services/web_server.py, Reference Images.dc.html)

- **The render queue lives in the server** (`Batch`, `/api/batch`). State is
  `projects/<slug>/assets/batch.json`; it resumes after a server restart,
  retries when ComfyUI is down, appends when asked while running, stops
  between images. The page only shows progress and asks; a browser-driven
  batch dies with the tab.
- **Every render is verified** by a vision model (`qwen3-vl:8b` via ollama,
  `VERIFY_MODEL`) with a structured checklist derived from the item
  (`_checks_for`); counts are asked decomposed (刀 / 刀鞘 / 出鞘刀身 — asked
  plainly it said "1" for two swords). A failed check re-renders with the
  failures spelled out and a new seed, `VERIFY_RETRIES` times; verdicts are
  stored on the take and shown on the tile (✓ 已核 / ⚠ n). The vision model
  shares ollama with the 27B: while `/assets` or `/script` is generating, a
  verify call can wait 20 min (timeout is 1500 s). A skipped verdict
  (`ok: null`) is re-checked later by queueing the key with `mode: "check"`,
  which verifies the picture on disk and only renders if it fails. An empty
  or half-empty reply from the vision model is "no verdict" (`ok: null`),
  never "everything failed" — one empty reply once sent a correct image back
  for a needless re-render. The model thinks before answering and cannot be
  told not to, so give it a large `num_predict` (8000; a dark night interior
  used 6k tokens thinking and answered nothing at 4000) and a bounded
  `num_ctx` (8192 — unbounded, the 8B took 40 GiB of VRAM). An empty reply
  is retried once before it counts as "no verdict". **The request prefills the
  assistant turn with `{"<first key>":`** — that is what stops the reasoning
  loop: without it a busy wharf scene burned 13172 tokens in 5m50s and
  answered nothing, with it the same image scores in 15 s. The image goes to the
  model as a 1280-px JPEG, not the 1920×1080 PNG: full-size image tokens
  plus reasoning overflowed an 8k context and the answer was cut off. A failed check is asked a
  second time (temperature 0.4) and only problems seen twice trigger a
  re-render — the model once called one sheathed sword "two, one drawn".
  Accessories the description names (笔挂、刀鞘、系绳) are part of the object,
  not a second item — a correct brush once failed "物件 2 件" for its strap.
  An object's own lettering (a book's title slip, a seal, an inscription) is
  not a watermark: the text check asks only about text unrelated to the object.
  A picture accepted by hand: `/api/take` to make it live, then
  `/api/batch/skip {keys}` so the queue drops (or interrupts) its re-render.
  Corrections go at the FRONT of the prompt (`【必须】…`) and say what is
  there ("双手空着自然下垂，刀完整在鞘中"), never what is not; appended
  "不出鞘" survived two re-renders of a drawn sword.
- **Every take is kept** (`assets/images/_takes/<asset>/<n>.png` + sidecar);
  the live file is the chosen take. Re-renders use a random seed and record
  it. Image URLs carry `?t=mtime` so a re-render actually shows.
- The page never uses `String.replace` to fill the workflow template — it hit
  the `_comment` first and sent the literal `__PROMPT__` to ComfyUI for weeks.
  Assign `wf["20"].inputs.text` directly.

- Reference plates have 日/夜/晨 in their file names; every server-side fetch of
  a `/media/...` URL must percent-encode the path (`h3_server._materialize_image`
  returned 400 "'ascii' codec can't encode" for every night plate).

## This machine (launch.sh)

- 128 GB Strix Halo, BIOS split 96 GiB VRAM / 31 GiB host. The host side is
  the wall: ComfyUI keeps host copies of loaded models (TE 11.7 GB + UNet
  18.8 GB > 31 GiB), so `--cache-none` is the *fast* setting and every
  "keep models resident" flag was slower (all measured; see the comment block
  in launch.sh). Real fix is the BIOS UMA split, not a flag.
- ComfyUI runs `--use-pytorch-cross-attention` with
  `TORCH_ROCM_AOTRITON_ENABLE_EXPERIMENTAL=1` (torch disables its flash /
  mem-efficient SDPA on this GPU without it). Split attention needed 54.9 GiB
  for one 3-second MiniMax-H3 clip; SDPA needs 0.5 GiB for the same 131k-token
  attention and is faster on images too (42.2 vs 49.0 s/step, same plate).
- ComfyUI runs with `TORCH_BLAS_PREFER_HIPBLASLT=0`: hipBLASLt on gfx1151
  lacks kernels for some MiniMax-H3 GEMM shapes ("getKernel failed … named
  symbol not found" → segfault mid-sample, 2026-09-07); rocBLAS has them.
- Sampling is 49 s/step for Flux.2 at 1920×1080 here; loading is ~50 s of a
  ~440 s image. Speed levers are steps and size, not memory flags.
