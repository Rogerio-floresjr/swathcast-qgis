import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qgs_app():
    from qgis.testing import start_app

    app = start_app()
    from swathcast_qgis.i18n import install_translator

    app._swathcast_en = install_translator("en")  # English plural forms, as in QGIS
    return app
