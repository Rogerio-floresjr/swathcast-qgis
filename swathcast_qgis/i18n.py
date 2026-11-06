"""Pick and install the plugin's translation for the QGIS locale."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from qgis.PyQt.QtCore import QCoreApplication, QTranslator

I18N = Path(__file__).resolve().parent / "i18n"


def install_translator(locale: str) -> Optional[QTranslator]:
    """pt_BR/pt get Portuguese; every other locale gets English, whose .qm only adds
    the plural forms ('1 pass', '2 passes') the source strings cannot express."""
    for name in (locale, locale[:2], "en"):
        qm = I18N / f"swathcast_{name}.qm"
        if name and qm.exists():
            translator = QTranslator()
            if translator.load(str(qm)):
                QCoreApplication.installTranslator(translator)
                return translator
    return None
