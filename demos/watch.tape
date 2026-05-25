# Live session tail: the FSM advancing step by step as the agent drives it.
# Paces a deterministic playback in the background, then tails it.
#   vhs demos/watch.tape
Output demos/watch.gif

Set Theme "rose-pine"
Set FontSize 14
Set Width 1340
Set Height 760
Set Padding 18
Set TypingSpeed 18ms

Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1"
Enter
Type `export PS1="$ "`
Enter
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type "python scripts/playback.py >/dev/null 2>&1 &"
Enter
Sleep 2s
Type "clear"
Enter
Show

Type "theodosia watch -p phoebe"
Sleep 500ms
Enter
Sleep 13s
