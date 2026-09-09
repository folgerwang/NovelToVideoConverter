voices/ -- reference clips for CosyVoice2 (zero-shot only: no built-in speakers).

A voice is a PAIR of files:
    <name>.wav   3-10 s of clean mono speech, 16 kHz or better, no music/noise
    <name>.txt   the EXACT transcript of that wav, nothing else

The web app asks for the voice named "storyteller".

storyteller.wav here is a PLACEHOLDER -- it is CosyVoice's own demo clip
(a young female voice) shipped with the repo, dropped in so the pipeline is
testable end to end. Replace both files with your own narrator to change how
the film sounds; no restart is needed, the server reads the pair per request.
