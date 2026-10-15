# Bookreel — rules that must not regress

These were each earned by a wrong picture. They live in code; `docs/rules/`
says where, so nobody "simplifies" them away. Chinese labels are the ones the
console shows.

**This file is the index. The rules are in `docs/rules/*.md`** - twenty topic
files, each entry with the defect that bought it, what was measured, and the
function it lives in.

- **Before changing code, a prompt or a queue in an area below, READ its
  file(s) first, whole.** Then `grep -rn "<function or word>" docs/rules/` for
  whatever you are about to touch: a rule is filed where it was found, which
  is not always where you would look (fight foley is in `video-prompt.md`,
  hot-reload in `chapter-film.md`).
- **Before diagnosing a defect** (a caption, a duplicate person, a wrong
  costume, a silent door), grep the symptom first. Most have happened before.
- **A new rule goes in its topic file**, written like the others: date, what
  was seen, what was measured, where in code. Add a line HERE only for a new
  always-on principle or a new file. Do not let this file grow back.

## Always true, whatever you are touching

- **No working negative prompt** on Flux by default (BasicGuider) or on h3
  (cfg 1.0, ZeroOut). Two levers only: say the true thing positively, and
  strip the word (`filmwords`, `look`). Real negatives exist only on character
  sheets and location plates (CFGGuider, cfg 2.0), Stable Audio and ACE-Step.
- **Never negate a noun** - 没有旗帜 draws flags, 无佩剑 a sword, "no vocals"
  vocals. Cut the word, or say what IS there.
- **Name the object, not the word** (油灯 → 陶碟油灯…, every window 木格糊纸,
  剑 → the straight sword described grip first). Shape words for light (窄条,
  光锥, 碎斑) and the word 月 become fixtures and moons.
- **World context is composed in code** (era, place, ethnicity, gender, age,
  costume), never left for the LLM to remember.
- **h3 paints whatever is not a drawable thing in the world**: labels, colons,
  digits, Latin, cast names, date + place, camera and frame talk, statements
  about the picture, quoted speech, stage directions. A video prompt holds
  people, clothes, objects, light and action only; `audit_video_prompt` runs
  last on the exact string and errs long. Audit RENDER-TIME text, not just
  what is stored.
- **Prompts are built at render time**; the script on disk stays what the LLM
  wrote, so a rule change reaches every queued shot.
- **The checker advises; the person decides.** Never discard a flagged clip or
  frame (it is a take, or moved to `_rejected/`); no automatic re-render
  (`VERIFY_RETRIES` 0, `CLIP_AUTO_FIX` off); checker text is never a prompt;
  the person's 重出意见 is the only correction. `ok: null` is NOT a pass -
  re-check before a render. A bulk button never redraws a picture that passed.
- **Deleting is moving** (`_trash/`, `_rejected/`); every take is kept. A
  film is never overwritten: the old one goes to `film/_versions/`.
- **A pass that costs GPU measures its own result** - lip-sync logged ✓ for
  six GPU-hours without changing a pixel.
- **Clips chain.** Same people in the same clothes from one clip to the next;
  the chain is read off disk in script order; each person's costume sits
  inside their own noun phrase; one plate view per location.
- **The sound is never cut.** A voiced shot is as long as its line; `apad`
  always gets `whole_dur`; audio is `-c:a copy` wherever it can be. **A shot
  with lines is never rendered mute:** the voice is step 1; a dead :9100 is
  restarted and waited for, and if it stays dead the queue STOPS with the
  shot at its head (`VoiceUnavailable`) - it never answers "no lines".
- **Queues live in the server**, state on disk, resumed on restart. No wait is
  unbounded. A failed side pass (film join, foley, music, grade, lip-sync)
  loses that pass, never the shot or the queue.
- **Rule modules hot-reload** (filmwords, fightcut, foley, look, stage,
  shotqa): put a new rule there. A web_server edit needs a restart - BETWEEN
  shots, clearing ComfyUI's queue first. Before blaming a rule that is already
  fixed, compare `ps -o lstart -p $(cat logs/web.pid)` with the file's mtime.
- **This machine:** `./launch.sh comfy-stop` before generating text.
  `launch.sh start` is TOTAL - use `restart-one <svc>`. No big download or
  CosyVoice load beside a render. Lip-sync runs with ComfyUI stopped.
  `journalctl -k` is where a wedge shows.
- **Geometry is load-bearing:** canvas 1312x736, sides multiples of 32,
  segments of 90 frames (17n+5), stall ceilings calibrated to ~27 min a
  segment. Read `previz.md` and `clips.md` before moving any of them.
- **Small traps:** routes defined after `if __name__ == "__main__":` never
  register; ffmpeg temp files are `<name>.part.mp4`; percent-encode `/media`
  paths (日/夜/晨); ComfyUI's POST /queue and /interrupt return an empty body;
  a `.dc.html` component takes no commas between methods and booleans as
  `{{ true }}` - `node --check` its script block after every edit.
- **Measured and ruled out - do not spend the GPU again:** NAG / a negative
  for h3; the Fun ControlNet on this base model (adaln mismatch - previz is
  for the person's eyes only); raising the canvas for resolution; ACE-Step for
  foley; synthesized blade clashes; the aligner for subtitle timing; IndexTTS
  as narrator; an octave of formant shift; a bright-pixel caption detector.

## The files

| `docs/rules/` | What is in it | Read before touching |
|---|---|---|
| `prompts.md` | Blueprint and reference images: `_anchor`/`_anchored`, 4 character views, 9/5 location views (`_site_kind`, `VIEW_PARTS`), `_OBJECTS`, scars, `strip_absent/states/pose/carried/face`, `UNIFORM_RULE` and faction colours, carried props → 物品栏, white board, back view, `note_for_sheet`, plate/sheet negatives and CFG, night light, `look.py` (novel-derived positive/negative), `_checks_for`, `/assets` completeness, `_covers_speech` | story_server `/blueprint` `/assets` `/script` require; sheet text in filmwords; look.py; the verifier's checklist |
| `import-novel.md` | `/import`: parts, digests, `is_group` crowds, `_age_words`, book chapters, `AutoRun` (载入小说), `build_book_films` | import, autorun, crowd counting |
| `rendering.md` | Image queue `Batch`, vision verifier settings (prefill, num_predict, 1280 px), takes, 认可 / 重出 / 重出意见, editable prompts (`/api/prompt`), `_passed_keys`, orphaned ComfyUI jobs | `Batch`, `_verify`, Reference Images page |
| `previz.md` | `stage.py` / `previz.py`: room coordinates, 内/外 rooms, `solve_camera`, lock-off and push, motion gate (`previz_moves`), control video (crop, cold shots, withheld), ControlNet conversion and why it has never rendered, `ensure_first_frame`, head counts (pre-flight, `clip_headcount`, `cast_count`), `shot_seconds` | stage, previz, first frames, head count, `SEG_SECONDS`, canvas |
| `clips.md` | `h3_server` / `VideoBatch`: stall deadlines, wedge and zombie recovery, `comfy_pid`, retries on a new seed, `apad=whole_dur`, canvas budget and `_trim_to_format` | h3_server, the render loop's waits, launch.sh comfy-restart, canvas |
| `lipsync.md` | LatentSync 1.6: own venv, dialog-only driving wav, new take, ComfyUI stopped, no-face frames, 24 fps patches, the tracer's stdout (two blind runs), `redo` from the source take | lipsync.py, `LipsyncBatch`, third_party/LatentSync |
| `stop-all.md` | `/api/stop-all`: graceful vs hard, queues not services | `stop_everything` |
| `upscale-4k.md` | Separate queue, idempotent, streamed frames, cover-crop, spandrel inference tensors | upscale.py, `UpscaleBatch` |
| `video-prompt.md` | What h3 is told: `_film_prompt`, `_segment_prompt`, `_people_clause`, `wear_clause`, mouth clause and cold-start lead, `action_beats`, filmwords substitutions (数字, `strip_camera`, `strip_speech`, jargon), `audit_video_prompt`, caption guard, narration cap and `inner` lines, `_facing_clause` (who talks to whom, face to face, listener resolved in code). ALSO: fight and crowd foley (`make-fight-foley.py`, `sfx_plan(grade=)`, 喊好), `shotqa` review, `fighters` / `fighter_check`, `fightcut.ripple`, first-frame staleness | anything that builds or filters a video prompt; shotqa; fight foley timing |
| `chapter-film.md` | `build_chapter_film`, title card, SRT offset, Range requests, music spans mixed on the film. ALSO: `_hot_reload`, clip 重出 in shot order, restarting web_server mid-render | film join, `_title_clip`, `_mix_music`, queue order, restarts |
| `narration.md` | CosyVoice3 RL engine, `_prompt_text`, instruct, why the voice is the reference clip, narrator_male, formant shift | tts_server, `BOOKREEL_TTS_MODEL`, narrator voices |
| `subtitles.md` | `chapter_srt`, `_clamp_cues`, `_speech_map`, `_snap_cues`, no picture cap on a voiced shot | cues, SRT/VTT, shot length ceilings |
| `action-scenes.md` | Fights: the 分镜 fight rules (rounds, arc, footwork, floor not a line, three layers, kicks), `_performance_clause`, `unfreeze`, `is_combat` / `_energy_clause`, `beats_seconds`, `JIAN_LOOK`, `fightcut` (speed, ramp, hits, crop camera, `_raw/`), `strip_glow`, orbit, `_film_dips`, first-frame angle and `FF_COMBAT`, `circle_fight` | fightcut.py, fight prompts, the /script fight section, soft transitions |
| `cutting.md` | Checker flags only (`_rejected/`), continuity (`_inherit_ratio`, `_continuity_ref`, `one_choice`, `_published_prev`), per-person costume, one plate view per scene (`_pick_first_frame`), `first_frame_refs`, size varies / angle holds, `scripts/check-cuts.py` | frame inheritance, first-frame references, the 分镜 prompt's cutting rules |
| `2026-10-06-faces-noise-mouths.md` | Bust-crop refs (`_bust_crop`), `_speaker_clause`, murmur levels and ducking (`_place_loops`, `SFX_DEDUP_SECONDS`, `SFX_ONE_MAX`), `_cut_picture` tail trim, lip-sync face hint / min face / `mouth-trace.py`, `strip_speech` 后 forms, `soften_face` | face consistency, foley levels, voiced-shot length, lip-sync verdicts |
| `casting.md` | `casting.json`, `_voice_for`, voice library (upload, transcribe, save, delete-by-move), `_voice_version`, auditions on a character's own line, grouping by work, `.dc.html` traps | 配音 panel, `/api/voice/*`, voices/ |
| `foley.md` | `foley.py` closed vocabulary and templates, `look.materials`, Stable Audio (T5-base, head click, `sfx_extract`), shelves by profile, `.wav` wins, `SFX_SYNTH`, Mixkit recordings, interiors and scene offsets, Mandarin cheer and murmur | foley.py, `SfxBatch`, `sfx_plan`, `_mix_sfx`, sfx/ |
| `music.md` | `MusicBatch`, one bed per location, never replayed, 240 s ceiling, English tag vocabulary (`look.music`, `instrument_tag`), `MUSIC_NEGATIVE` | music beds, ACE-Step tags |
| `wedge-2026-09-10.md` | What the text-encoder wedge is NOT (nine things ruled out) | re-diagnosing a hung render |
| `machine.md` | Strix Halo memory split, comfy-stop before text, OOM from downloads, `launch.sh start` vs `restart-one`, ComfyUI flags and their measurements | launch.sh, service flags, memory |

## By code file

- `services/story_server.py` → prompts, import-novel, cutting, action-scenes,
  video-prompt (narration / `inner` rules), previz (`/stage`)
- `services/web_server.py` → `Batch`: rendering · `VideoBatch._render`: clips,
  video-prompt, cutting, previz, action-scenes, 2026-10-06 · film:
  chapter-film, subtitles · sound: foley, music, narration, casting ·
  lipsync, upscale-4k, stop-all
- `services/filmwords.py` → prompts, video-prompt, action-scenes, 2026-10-06
- `services/look.py` → prompts, foley, music
- `services/stage.py`, `previz.py` → previz, action-scenes
- `services/fightcut.py` → action-scenes, video-prompt
- `services/foley.py`, `scripts/make-fight-foley.py` → foley, video-prompt,
  2026-10-06
- `services/shotqa.py` → video-prompt, 2026-10-06
- `services/h3_server.py` → clips, previz (control), video-prompt (NAG)
- `services/tts_server.py`, `voices/` → narration, casting
- `*.dc.html` → rendering, casting
- `launch.sh` → machine, clips, wedge-2026-09-10
