"""Plugin life cycle as QGIS drives it: classFactory → initGui → open dock → unload."""
import pytest

pytest.importorskip("qgis.core")


def test_load_open_unload(qgs_app, monkeypatch):
    from qgis.core import QgsApplication, QgsSettings
    from qgis.testing.mocked import get_iface

    import swathcast_qgis
    from swathcast_qgis import dock as dock_mod
    from test_dock import FakeTransport, wait_for

    from swathcast_qgis import welcome

    shown = []
    monkeypatch.setattr(welcome.WelcomeDialog, "exec", lambda self: shown.append(self))
    monkeypatch.setattr(dock_mod, "QgisTransport", FakeTransport)
    QgsSettings().remove(welcome.SEEN_KEY)
    QgsSettings().setValue("locale/userLocale", "pt_BR")
    iface = get_iface()
    plugin = swathcast_qgis.classFactory(iface)
    assert plugin.translator is not None  # the pt_BR .qm was found and installed
    plugin.initGui()
    assert plugin.action.text() == "Passagens de satélites (SwathCast)"
    iface.addToolBarIcon.assert_called_once()

    plugin.action.setChecked(True)
    assert plugin.dock is not None
    iface.addDockWidget.assert_called_once()
    wait_for(lambda: plugin.dock._loader is None)
    assert plugin.dock.btn_run.text() == "Buscar passagens"
    assert len(shown) == 1  # first opening shows the welcome notice, in Portuguese
    assert shown[0].windowTitle() == "Boas-vindas ao SwathCast para QGIS"
    from qgis.PyQt.QtWidgets import QLabel

    # the notice is the Info tab: the logo and the author are in it
    labels = shown[0].findChildren(QLabel)
    assert any(lab.objectName() == "logoCard" for lab in labels)
    assert any("rogerio.floresjr@gmail.com" in lab.text() for lab in labels)
    plugin.action.setChecked(False)
    plugin.action.setChecked(True)
    QgsApplication.processEvents()
    assert len(shown) == 1  # only once
    plugin.dock.btn_about.click()
    assert len(shown) == 1  # the help button opens the Info tab, not the notice again
    assert plugin.dock.stack.currentIndex() == 3

    plugin.unload()
    assert plugin.dock is None and plugin.action is None and plugin.translator is None
    iface.removeToolBarIcon.assert_called_once()
    QgsSettings().remove("locale/userLocale")
    QgsSettings().remove(welcome.SEEN_KEY)
    QgsApplication.processEvents()


def test_icons_exist_for_both_themes(qgs_app):
    from pathlib import Path

    from swathcast_qgis import theme

    pkg = Path(theme.__file__).parent
    meta = (pkg / "metadata.txt").read_text(encoding="utf-8")
    icon = next(line.split("=", 1)[1] for line in meta.splitlines() if line.startswith("icon="))
    assert (pkg / icon).is_file()
    assert theme.logo_path(dark=True).endswith("swathcast_icon_dark.svg")
    assert theme.logo_path(dark=False).endswith("swathcast_icon_light.svg")
    assert all(Path(theme.logo_path(dark=d)).is_file() for d in (True, False))


def test_design_tokens_match_the_shared_design_folder():
    """The plugin ships a copy of ~/BDC/swathcast-design; it must not drift from it."""
    import subprocess
    import sys
    from pathlib import Path

    design = Path(__file__).resolve().parents[2] / "swathcast-design"
    if not design.is_dir():
        pytest.skip("shared design folder not present (CI)")
    plugin = Path(__file__).resolve().parents[1]
    run = subprocess.run([sys.executable, str(design / "scripts" / "sync_plugin.py"),
                          str(plugin), "--check"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout


def test_info_links_to_the_privacy_notice_and_terms_in_the_users_language(qgs_app):
    from qgis.core import QgsApplication
    from qgis.PyQt.QtWidgets import QLabel

    from swathcast_qgis.i18n import install_translator
    from swathcast_qgis.info import info_page

    def links():
        page = info_page([])  # keep a reference: the labels die with the page
        return " ".join(lab.text() for lab in page.findChildren(QLabel))

    english = links()
    assert "https://swathview.pages.dev/privacy'" in english
    assert "https://swathview.pages.dev/terms'" in english
    pt = install_translator("pt_BR")
    try:
        portuguese = links()
    finally:
        QgsApplication.removeTranslator(pt)
    assert "https://swathview.pages.dev/privacidade'" in portuguese
    assert "https://swathview.pages.dev/termos'" in portuguese
