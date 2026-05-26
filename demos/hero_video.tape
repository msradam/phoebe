# LinkedIn hero video: the real Kimi K2.6 run, narrated on rails by Theodosia,
# full render (query + result signal + rationale + gated conclusion) so the data
# flow is visible. Same recipe as hero_sonnet, output as mp4, Kimi trace.
# Re-record with: vhs demos/hero_video.tape
Output demos/hero_video.mp4

Set Theme "rose-pine"
Set FontSize 15
Set Width 1080
Set Height 760
Set Padding 20
Set TypingSpeed 1ms

Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1"
Enter
Type `export PS1="$ "`
Enter
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type "clear"
Enter
Type "python scripts/narrate.py"
Enter
Sleep 1500ms
Show
Sleep 30s
