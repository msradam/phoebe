# One-line-per-step session log: the walk, with the loop-guard refusal in red.
#   python scripts/playback.py   # first, to create a session
#   vhs demos/logs.tape
Output demos/logs.gif

Set Theme "rose-pine"
Set FontSize 14
Set Width 1340
Set Height 560
Set Padding 20
Set TypingSpeed 22ms

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

Type "theodosia logs -p phoebe"
Sleep 700ms
Enter
Sleep 6s
