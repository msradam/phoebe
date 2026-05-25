# Light-theme variant of the compact hero, for the docs light mode. Same run,
# neutral #fafafa background so it sits on the v14 light page. The site swaps
# this for hero_tall.gif (dark) based on the active theme.
# Re-record: THD_HERO=1 THD_LIGHT=1 vhs demos/hero_tall_light.tape
Output demos/hero_tall_light.gif

Set Theme { "name": "thd-light", "background": "#fafafa", "foreground": "#0a0a0a", "cursor": "#573e8a", "selection": "#efebf7", "black": "#0a0a0a", "red": "#b4637a", "green": "#5b8d68", "yellow": "#c47d10", "blue": "#3d7a86", "magenta": "#6e54a6", "cyan": "#56949f", "white": "#e6e6e2", "brightBlack": "#7a7a76", "brightRed": "#b4637a", "brightGreen": "#5b8d68", "brightYellow": "#c47d10", "brightBlue": "#3d7a86", "brightMagenta": "#6e54a6", "brightCyan": "#56949f", "brightWhite": "#fafafa" }
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
Type "export THD_HERO=1 THD_LIGHT=1"
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
