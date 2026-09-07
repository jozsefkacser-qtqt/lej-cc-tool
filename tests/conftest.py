"""Test fixtures.

The sample workbooks are generated at the start of each session rather
than committed. They are synthetic data derived from the real exports, and
generating them keeps binaries out of git and guarantees CI has them.
"""

from pathlib import Path

import pytest
from make_fixtures import write_all


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_all(tmp_path_factory.mktemp("workbooks"))
