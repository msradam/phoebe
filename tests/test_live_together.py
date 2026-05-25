"""Opt-in live check: does the real model + provider return tool calls the
runner can parse?

This is the cheap pre-grind gate. It makes ONE real chat completion (a few
cents) against the configured model and asserts extract_tool_calls finds a
usable call, whether structured or in a leaked native format. It is skipped
unless RUN_LIVE=1 and the provider keys are set, so the default suite stays
free and offline.

Run before a graded benchmark:

    RUN_LIVE=1 \\
    OPENAI_API_BASE=https://api.together.xyz/v1 OPENAI_API_KEY=$TOGETHER_API_KEY \\
    LIVE_MODEL=openai/moonshotai/Kimi-K2.6 \\
    uv run pytest tests/test_live_together.py -v -m live
"""

from __future__ import annotations

import os

import pytest

from phoebe.harbor.agent_runner import FSM_TOOLS, extract_tool_calls

pytestmark = pytest.mark.live

_SKIP = not os.environ.get("RUN_LIVE") or not os.environ.get("OPENAI_API_KEY")


@pytest.mark.skipif(_SKIP, reason="set RUN_LIVE=1 and provider keys to run the live check")
@pytest.mark.asyncio
async def test_provider_returns_parseable_tool_call():
    import litellm

    model = os.environ.get("LIVE_MODEL", "openai/moonshotai/Kimi-K2.6")
    messages = [
        {
            "role": "system",
            "content": "You investigate incidents. Call exactly one tool: start_investigation.",
        },
        {
            "role": "user",
            "content": "Begin investigating: error rates jumped across services a few hours ago.",
        },
    ]
    resp = await litellm.acompletion(
        model=model, messages=messages, tools=FSM_TOOLS, tool_choice="auto"
    )
    msg = resp.choices[0].message
    calls = extract_tool_calls(msg)
    assert calls, (
        f"model returned no parseable tool call. content was:\n{getattr(msg, 'content', None)!r}"
    )
    assert calls[0].name in {t["function"]["name"] for t in FSM_TOOLS}
