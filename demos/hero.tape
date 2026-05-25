# Hero: a real Kimi K2.6 run, driven on rails by Theodosia, narrated.
# Replays the captured ground-truth trace (scripts/hero_trace.json) at a
# readable pace. Re-record with: vhs demos/hero.tape
Output demos/hero.gif

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
# Launch while hidden; narrate.py clears the screen as its first action, so the
# recording opens directly on the narration with no prompt or command line.
Type "python scripts/narrate.py"
Enter
Sleep 1500ms
Show
Sleep 30s
