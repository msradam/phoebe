"""Unit tests for the runner's tool-call extraction.

This is the boundary that broke a live run: Kimi K2.x emitted tool calls in
its native token format inside message content instead of the structured
tool_calls field, and the loop ended with an empty answer. These tests pin
both paths so a parse miss can never silently end a run again.
"""

from __future__ import annotations

from types import SimpleNamespace

from phoebe.harbor.agent_runner import (
    assistant_message,
    atif_steps_from_messages,
    extract_tool_calls,
)


def _structured(name: str, arguments: str, call_id: str = "c0"):
    fn = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=call_id, function=fn)])


def _leaked(name: str, args_json: str):
    body = (
        f"<|tool_calls_section_begin|><|tool_call_begin|>functions.{name}:0"
        f"<|tool_call_argument_begin|>{args_json}<|tool_call_end|><|tool_calls_section_end|>"
    )
    return SimpleNamespace(content=body, tool_calls=None)


def test_structured_tool_calls():
    calls = extract_tool_calls(_structured("query_metrics", '{"promql": "up"}'))
    assert len(calls) == 1
    assert calls[0].name == "query_metrics"
    assert calls[0].arguments == {"promql": "up"}


def test_leaked_tokens_in_content_are_recovered():
    msg = _leaked("advance_phase", '{"to": "verify", "rationale": "metrics and logs agree"}')
    calls = extract_tool_calls(msg)
    assert len(calls) == 1
    assert calls[0].name == "advance_phase"
    assert calls[0].arguments == {"to": "verify", "rationale": "metrics and logs agree"}


def test_leaked_conclude_is_recovered():
    args = '{"primary_service": "payment-service", "root_cause": "pool exhaustion", "final_answer": "x"}'
    calls = extract_tool_calls(_leaked("conclude", args))
    assert len(calls) == 1
    assert calls[0].name == "conclude"
    assert calls[0].arguments["primary_service"] == "payment-service"


def test_malformed_leaked_json_degrades_gracefully():
    # The exact shape from the failed live run: truncated/invalid JSON. It must
    # still surface as a call (so the action runs and the loop continues),
    # carrying the raw text rather than crashing.
    bad = '{"promql":"count by (__name__) ({job=~"payment", x})"'
    calls = extract_tool_calls(_leaked("query_metrics", bad))
    assert len(calls) == 1
    assert calls[0].name == "query_metrics"
    assert "_raw" in calls[0].arguments


def test_no_calls_returns_empty():
    assert (
        extract_tool_calls(SimpleNamespace(content="just prose, no tools", tool_calls=None)) == []
    )


def test_atif_steps_surface_response_and_evidence():
    # The o11y-bench transcript parser only reads steps with a "source" field,
    # and grades the response text. Confirm the conversation converts and the
    # final answer + tool evidence are present.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "investigate"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "query_metrics", "arguments": '{"promql": "up"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"rows": 3, "job": "payment-service"}'},
    ]
    steps = atif_steps_from_messages(messages, final_answer="payment-service is primary; 5xx 3%.")
    sources = [s["source"] for s in steps]
    assert sources == ["system", "user", "agent", "agent"]
    # the agent tool step carries the call and the tool result as an observation
    agent_step = steps[2]
    assert agent_step["tool_calls"][0]["function_name"] == "query_metrics"
    assert agent_step["observation"]["results"][0]["source_call_id"] == "c1"
    # the final answer is the closing agent message the grader reads
    assert steps[-1]["message"] == "payment-service is primary; 5xx 3%."


def test_assistant_message_strips_leaked_tokens_from_content():
    msg = _leaked("query_logs", '{"logql": "{job=\\"x\\"}"}')
    calls = extract_tool_calls(msg)
    rebuilt = assistant_message(msg.content, calls)
    assert "<|tool_call" not in rebuilt["content"]
    assert rebuilt["tool_calls"][0]["function"]["name"] == "query_logs"
