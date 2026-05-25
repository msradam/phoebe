"""Data-driven tests for the FSM's methodology gates, via Burr's harness.

Each case in cases/gates.json names an action and its inputs (reserved
``_action`` / ``_inputs`` keys inside ``input_state``) and an expected
outcome: ``error_contains`` for a rejected transition, or state-key
assertions for an accepted one. Burr's ``pytest_generate_tests`` hook
(re-exported in conftest.py) parametrizes this from the file_name marker.
"""

from __future__ import annotations

import asyncio

import pytest
from burr.core import State

from phoebe.app import advance_phase, conclude

_ACTIONS = {"advance_phase": advance_phase, "conclude": conclude}


@pytest.mark.file_name("tests/cases/gates.json")
def test_gate_cases(input_state: dict, expected_state: dict) -> None:
    data = dict(input_state)
    action = _ACTIONS[data.pop("_action")]
    inputs = data.pop("_inputs", {})
    state = State(data)

    if "error_contains" in expected_state:
        with pytest.raises(ValueError) as excinfo:
            asyncio.run(action(state, **inputs))
        assert expected_state["error_contains"] in str(excinfo.value)
    else:
        new_state = asyncio.run(action(state, **inputs))
        for key, value in expected_state.items():
            assert new_state[key] == value
