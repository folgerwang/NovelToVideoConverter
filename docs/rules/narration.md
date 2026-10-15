# Narration (services/tts_server.py, voices/)

- **The engine is Fun-CosyVoice3-0.5B's RL checkpoint** (`BOOKREEL_TTS_MODEL`,
  default `cosyvoice3-rl`), not CosyVoice2. `models/cosyvoice3-rl` is symlinks
  onto `models/cosyvoice3` with `llm.rl.pt` in place of `llm.pt`, because the
  loader hardcodes the latter name. tts_server puts `third_party/CosyVoice3`
  on sys.path ahead of the old checkout and loads through `AutoModel`, which
  dispatches on which of `cosyvoice{,2,3}.yaml` the model dir holds - so
  rolling back to v2 is the env var, not a code change. Measured on this box,
  narrator_male, same lines: HNR 6.7/6.1/5.4/6.7 dB against v2's
  6.5/4.9/5.6/6.4, and ASR heard 争 and 日头 correctly where v2 gave 赠 and
  偏呢. Both still mangle 银锞子 and 琥珀色, and v3 read 三钱 as "30" once -
  numbers are its weak spot, not its strength.
- **v3 wants the reference transcript behind a system line**
  (`_prompt_text`): `You are a helpful assistant.<|endofprompt|>` + the .txt.
  Every example in its own repo passes one. v2 must NOT be given one - it
  would be read aloud as words to speak.
- **Changing the engine re-renders every voiced shot.** Narration length
  drives how long a voiced shot runs, so this variable is not a knob to try
  casually while a chapter is rendering: on 2026-09-15 switching it meant
  requeueing all 165 shots, ~423 segments, ~190 GPU-hours for 20 minutes of
  film.
- **instruct2 obeys on v3, and did not on v2.** `请用低沉、克制、缓慢的语气
  讲述。<|endofprompt|>` came back spoken in that manner (10.86 s against the
  plain take's 10.52), where this checkout's v2 SPEAKS the direction instead -
  which is why `BOOKREEL_NARRATION_INSTRUCT` is empty. If the narration still
  reads flat, that is now the lever to try before recording a human.
- **`inference_vc` survives the upgrade**, so the `.rhythm` voices still work:
  all 21 auditions regenerated on v3, `_st` ones included.
- **IndexTTS-2.5 is installed but not wired in** (`venvs/indextts`, python
  3.11 via uv because its pyproject is `>=3.10,<3.12`; torch
  2.9.1+rocm7.13 cp311 from the gfx1151 index - its torch==2.8 pin is
  conservative and ROCm works). It measured best on consonant energy (8.4% of
  1-4 kHz against v2's 6.5%) and read the test line correctly, and it is the
  only engine here that can separate delivery from the reference clip (an
  8-float emotion vector). Two reasons it is not the narrator: it needs the
  GPU - 118 s a line there, and it had not finished ONE line in 15 minutes on
  CPU - and narration is synthesized inside the video render loop, so a
  GPU-hungry TTS fights the video sampler on every shot. Its emotion vector at
  0.7 also threw the narrator from 127 Hz to 182 Hz; speaker similarity is
  what `emo_alpha` spends.
- **The voice IS the reference clip.** CosyVoice2 is zero-shot with no speaker
  table, so no setting makes the narrator male, older or slower - only a
  different `voices/<name>.wav` + `.txt` pair, named by `BOOKREEL_VOICE`. The
  `instruct` path cannot stand in: this checkout SPEAKS the direction instead
  of obeying it (2026-09-10, a 36-char line went 11.20 s -> 23.02 s, the extra
  11.8 s being the instruction read aloud). That is why
  `BOOKREEL_NARRATION_INSTRUCT` is empty.
- **Pick the clip that is already close, do not manufacture one.** The
  narrator is `narrator_male` (`scripts/make-narrator.sh`): CosyVoice's
  *other* shipped reference, `asset/cross_lingual_prompt.wav` - a real
  recording, 148 Hz, SenseVoice-NEUTRAL, full bandwidth - trimmed to its first
  three sentences and moved down 3 semitones. `narrator_neutral` is that trim
  untouched, 149 Hz.
- **Formants are a vocal tract, so they move two semitones, not twelve.** The
  old default `narrator_m12` was `storyteller` (CosyVoice's 262 Hz female
  demo, tagged HAPPY) at `rubberband=pitch=0.5:formant=shifted` - an octave.
  An octave of formant shift is not a man: the formant spacing works out to a
  **21 cm vocal tract**, which no human has, and it empties the voice - the
  1-4 kHz band that carries Mandarin's consonants fell from 48% of the energy
  to 2.7% and 4-8 kHz to zero. Measured on the synthesized narration
  (2026-09-13): m12 clones at **3.4 dB** harmonic-to-noise against
  narrator_male's **6.6**, and ASR heard its 崇祯 as 逢祯 and 苏州 as 福州
  while hearing narrator_male correctly. A male tract is ~19% longer than a
  female one - two semitones. `narrator_m6/m9/m12` are kept and must not be
  used.
- **This fixes "wrong person", not "flat".** Zero-shot copies the reference's
  prosody as well as its timbre, so the narration reads as evenly as the clip
  does. The new reference at least reads NEUTRAL rather than cheerful. A real
  recording in `voices/` is the only fix for the rest.


## 2026-10-15: the whole chapter came back mute, logged 无台词

- **Seen:** "the mouth movement just didn't have voice". Every published take
  of tianlongbabu ch01 reads `voice_seconds: 0.0, cues: []` - the twelve
  dialog shots (C01S06, S09, S13-S16, S18, S19, S21-S24) included, both the
  2026-10-10 takes and the 2026-10-14/15 re-render - and the chapter SRT is
  empty. The prompt still named the speaker (`_speaker_clause`), so h3 drew
  a talking mouth over silence.
- **Measured:** `journalctl -k`: the kernel OOM-killed tts_server (pid
  104859, `oom_score_adj:1000`) at 2026-10-10 06:10:13; tts.log ends with a
  successful 着！ at 06:08. From then on `_synth` got `Connection refused`
  and `_voice_track` returned `(None, 0.0, [])` - the same answer as "this
  shot has no lines" - with one `！… 旁白/台词未出` line in the queue log and
  the publish line saying **无台词**. ~16 GPU-hours of dialog shots rendered
  without their sound, twice.
- **Why it will recur:** tts_server carries `oom_score_adj=1000` on purpose
  (machine.md: it is what the kernel takes instead of the render). So the
  render must expect it dead.
- **Now (`VideoBatch._synth_or_stop`, `_tts_recover`, `_tts_up`,
  `VoiceUnavailable`, `TTS_START_WAIT` 300 s):** a line that cannot reach
  :9100 runs `launch.sh restart-one tts` and waits (bounded) for /health,
  then synthesizes; if the service stays down the shot raises
  `VoiceUnavailable`, `_run` puts it back at the HEAD of the queue and stops
  the queue (nothing after it is rendered mute either, nothing inherits
  across it). A service that is up but refuses one line (HTTP error, e.g.
  CosyVoice's `<|endofprompt|>` assertion) is asked once more and then fails
  THAT SHOT only. An empty wav fails the shot too. The voice is step 1,
  before any GPU is spent, so refusing costs nothing - the old comment
  ("losing the whole 56-minute render because :9100 was down") was wrong
  about where in the shot the voice is made.
- **Reading the log:** 无台词 on a shot that has a `dialog` in the script is
  this bug, nothing else. 旁白 0.0 秒 in the ▶ line on such a shot likewise.
- **Same day, second fault behind the same symptom: every INSTRUCTED line
  was HTTP 500 on v3** (`tts_server._instruct_text`). CosyVoice3's LLM
  asserts `151646 in text` (`<|endofprompt|>`), and on `inference_instruct2`
  the direction is that text; `_prompt_text` added the marker on the
  zero-shot path only. So an `inner` line (C01S06 不好，失态了, instruct
  请用很轻、很近…) and any dialog with `BOOKREEL_DIALOG_INSTRUCT` on died in
  the LLM thread - tts.log: `AssertionError: <|endofprompt|> not detected`.
  The marker is now appended on gen 3 when missing (the 2026-10-09 test
  above used exactly that suffix). Measured after: 7 chars → 1.6 s, the
  direction not read aloud. With the render fix above, a line the service
  refuses fails THAT SHOT (× …出不了声…HTTP Error 500) instead of going mute,
  which is how this one surfaced within a minute instead of after a day.
