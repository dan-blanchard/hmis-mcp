import pytest

from hmis_mcp.engine import Engine
from hmis_mcp.model import Model
from hmis_mcp.sample import generate
from hmis_mcp.source import CsvExportSource


@pytest.fixture(scope="session")
def export_dir(tmp_path_factory):
    return generate(tmp_path_factory.mktemp("export"))


@pytest.fixture(scope="session")
def model(export_dir):
    return Model(CsvExportSource(export_dir))


@pytest.fixture
def engine(model):
    # Fresh disclosure history per test; the loaded model is shared.
    return Engine(model)
