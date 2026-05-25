"""Render a captured investigation (scripts/hero_trace.json) as a paced,
readable narration for the hero gif. Reads the real trace produced by
capture_run.py and prints it step by step: a per-phase rail, the query the
model actually ran, the real result, the gate marker, and the model's own
rationale at each phase boundary. A refused step shows in red: the model
asked, the server said no.

    uv run python scripts/narrate.py            # play it, paced
    uv run python scripts/narrate.py --fast     # no pauses (quick look)
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

HERO = os.environ.get("THD_HERO") == "1"  # compact portrait layout for the hero gif

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

_args = [a for a in sys.argv[1:] if not a.startswith("--")]
_TRACE = Path(_args[0]) if _args else Path(__file__).parent / "hero_trace.json"


def _model_display(model: str) -> str:
    m = model.lower()
    if "kimi" in m:
        return "Kimi K2.6"
    if "sonnet" in m:
        return "Claude Sonnet 4.6"
    if "gpt" in m or "openai/o" in m:
        return model.split("/")[-1]
    return model.split("/")[-1]


# Rose Pine
BASE = "#191724"
TEXT = "#e0def4"
MUTED = "#908caa"
LOVE = "#eb6f92"
GOLD = "#f6c177"
ROSE = "#ebbcba"
PINE = "#31748f"
FOAM = "#9ccfd8"
IRIS = "#c4a7e7"

PHASE_COLOR = {"triage": FOAM, "diagnose": IRIS, "verify": GOLD, "conclude": PINE}
SKIP = {"list_datasources"}  # pure discovery; not part of the story

console = Console(width=52 if HERO else 100)
FAST = "--fast" in sys.argv


def pause(s: float) -> None:
    if not FAST:
        time.sleep(s)


def _tag(phase: str) -> Text:
    c = PHASE_COLOR.get(phase, MUTED)
    return Text(f" {phase.upper():<8} ", style=f"bold {BASE} on {c}")


def _query(args: dict[str, Any]) -> str:
    for k in ("expr", "query", "logql", "promql", "traceql", "traceID"):
        if args.get(k):
            return str(args[k])
    return ""


def _signal(tool: str, result: str) -> str:
    """An honest one-phrase reading of a real tool result (the full payload is
    truncated in the trace, so these are conservative surface signals)."""
    r = result
    if "bad_data" in r or "parse error" in r:
        return "query rejected (parse error)"
    m = __import__("re").search(r'"traceId"\s*:\s*"([0-9a-f]+)"', r)
    if m:
        return f"trace {m.group(1)[:10]}… resolved"
    if '"traces"' in r:
        return "error traces found"
    if '"data": []' in r:
        return "no series in window"
    if '"line"' in r or '"values"' in r or '"metric"' in r:
        return "data returned"
    return (r[:32] + "…") if len(r) > 32 else r


def _wrap(s: str, width: int, indent: str) -> str:
    return textwrap.fill(
        " ".join(s.split()),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
    )


def main() -> None:
    console.clear()  # wipe the launch command line so a recording opens clean
    trace = json.loads(_TRACE.read_text())
    steps: list[dict[str, Any]] = trace["steps"]

    console.print()
    if HERO:
        console.print(
            Panel(
                Text.assemble(
                    (_model_display(trace.get("model", "")), f"bold {ROSE}"),
                    ("\non rails by ", MUTED),
                    ("Theodosia", f"bold {IRIS}"),
                ),
                border_style=IRIS,
                title="[bold]Phoebe[/]  ·  SRE investigation",
                title_align="left",
                padding=(0, 1),
            )
        )
    else:
        console.print(
            Panel(
                Text.assemble(
                    ("incident   ", f"bold {MUTED}"),
                    (trace["incident"], TEXT),
                    ("\nagent      ", f"bold {MUTED}"),
                    (_model_display(trace.get("model", "")), f"bold {ROSE}"),
                    ("  driven on rails by ", MUTED),
                    ("Theodosia", f"bold {IRIS}"),
                    ("  (open Grafana toolset, gated phases)", MUTED),
                ),
                border_style=IRIS,
                title="[bold]Phoebe[/]  ·  live SRE investigation",
                title_align="left",
                padding=(0, 1),
            )
        )
    console.print()
    pause(2.2)

    phase = "triage"
    for st in steps:
        tool = st.get("tool", "")
        if tool in SKIP or tool == "start_investigation":
            continue
        if tool == "conclude":
            break
        if tool == "advance_phase":
            phase = st.get("args", {}).get("to", phase)
            why = st.get("args", {}).get("rationale", "")
            c = PHASE_COLOR.get(phase, MUTED)
            console.print(Text(f"   ──►  advance to {phase.upper()}", style=f"bold {c}"))
            if why and not HERO:
                wrapped = _wrap(why, 92, "             ").splitlines()[:3]
                if wrapped:
                    wrapped[-1] = wrapped[-1].rstrip(".") + " …"
                console.print(Text("\n".join(wrapped), style=f"italic {MUTED}"))
            console.print()
            pause(2.4)
            continue

        refused = st.get("status", "ok") != "ok"
        mark = "✗" if refused else "●"
        mark_c = LOVE if refused else PHASE_COLOR.get(phase, FOAM)
        q = _query(st.get("args", {}))
        result = st.get("result", "")
        if HERO:
            # Compact portrait row: phase rail + tool, no query/signal tail.
            console.print(
                Text.assemble(
                    _tag(phase),
                    (f"  {mark} ", f"bold {mark_c}"),
                    (f"{tool[:20]}", f"bold {TEXT}"),
                )
            )
            pause(1.1)
            continue
        if refused:
            note = result.split(":", 1)[0].split("(")[0].strip()
            tail = Text.assemble(("  refused · ", f"bold {LOVE}"), (note[:24], LOVE))
        else:
            tail = Text.assemble(("  → ", MUTED), (_signal(tool, result), ROSE))
        console.print(
            Text.assemble(
                _tag(phase),
                (f"  {mark} ", f"bold {mark_c}"),
                (f"{tool:<21} ", f"bold {TEXT}"),
                (q[:32], MUTED),
                tail,
            )
        )
        pause(1.3)

    pause(1.2)
    primary = trace.get("primary_service") or "?"
    root = " ".join((trace.get("root_cause") or "").split())
    if HERO:
        console.print(
            Panel(
                Text.assemble(
                    ("primary at fault  ", f"bold {MUTED}"),
                    (primary, f"bold {GOLD}"),
                ),
                border_style=PINE,
                title="[bold]conclude[/]  ✓ gated",
                title_align="left",
                padding=(0, 1),
            )
        )
    else:
        console.print(
            Panel(
                Text.assemble(
                    ("primary at fault   ", f"bold {MUTED}"),
                    (primary, f"bold {GOLD}"),
                    ("\nroot cause         ", f"bold {MUTED}"),
                    (root[:150] + ("…" if len(root) > 150 else ""), TEXT),
                ),
                border_style=PINE,
                title="[bold]conclude[/]  ✓  gate: verify · 2+ backends · verify-phase probe",
                title_align="left",
                padding=(0, 1),
            )
        )
    console.print()
    pause(3.0)  # let the conclusion rest on screen at the end of the recording


if __name__ == "__main__":
    main()
