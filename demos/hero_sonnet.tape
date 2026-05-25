# Hero variant: a real Claude Sonnet 4.6 run, driven on rails by Theodosia.
# Re-record with: vhs demos/hero_sonnet.tape
Output demos/hero_sonnet.gif

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
Show

Type "python scripts/narrate.py scripts/hero_trace_sonnet.json"
Enter
Sleep 15s
