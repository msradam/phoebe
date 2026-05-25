# The FSM invariant as a circuit: hub topology, entry, loops, terminal.
#   vhs demos/circuit.tape
Output demos/circuit.gif

Set Theme "rose-pine"
Set FontSize 16
Set Width 1280
Set Height 600
Set Padding 22
Set TypingSpeed 22ms

Hide
Type "export VIRTUAL_ENV_DISABLE_PROMPT=1"
Enter
Type `export PS1="$ "`
Enter
Type "source .venv/bin/activate >/dev/null 2>&1 || true"
Enter
Type "theodosia render o11y_fsm.app:build_application --app-dir src >/dev/null 2>&1"
Enter
Sleep 4s
Type "clear"
Enter
Show

Type "theodosia render o11y_fsm.app:build_application --app-dir src"
Sleep 700ms
Enter
Sleep 4s
