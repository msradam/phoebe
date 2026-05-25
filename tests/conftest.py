"""Enable Burr's data-driven test harness.

Re-exporting ``pytest_generate_tests`` makes Burr's hook
(burr.testing.pytest_generate_tests) parametrize any test that takes
``input_state`` and ``expected_state`` from a ``file_name`` marker.
"""

from burr.testing import pytest_generate_tests  # noqa: F401
