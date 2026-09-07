from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture
def data_dir() -> Path:
    return DATA
