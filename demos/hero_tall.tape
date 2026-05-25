# Compact portrait hero gif: the same Kimi run, narrated, sized to fit the
# landing hero's right column. Re-record with: THD_HERO=1 vhs demos/hero_tall.tape
Output demos/hero_tall.gif

Set Theme "rose-pine"
Set FontSize 17
Set Width 860
Set Height 600
Set Padding 28
Set TypingSpeed 1ms

Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1"
Enter
Type `export PS1="$ "`
Enter
Type "export THD_HERO=1"
Enter
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type "clear"
Enter
Type "python scripts/narrate.py"
Enter
Sleep 1500ms
Show
Sleep 25s
