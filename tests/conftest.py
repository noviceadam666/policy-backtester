import pytest

from fakehermes import Fleet


@pytest.fixture
def fleet(tmp_path) -> Fleet:
    return Fleet(tmp_path / "hermes")
