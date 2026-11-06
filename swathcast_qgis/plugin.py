"""Plugin entry: a toolbar button and a Web-menu item that open the SwathCast dock."""
from __future__ import annotations

from qgis.PyQt.QtCore import QCoreApplication, Qt, QTimer
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction

from qgis.core import QgsSettings

from .i18n import install_translator


class SwathcastPlugin:
    def __init__(self, iface) -> None:
        self.iface = iface
        self.dock = None
        self.action = None
        self.translator = install_translator(QgsSettings().value("locale/userLocale", "") or "")

    def tr(self, text: str) -> str:
        return QCoreApplication.translate("SwathcastPlugin", text)

    def initGui(self) -> None:
        from .theme import logo_path

        self.action = QAction(QIcon(logo_path()),
                              self.tr("Satellite overpasses (SwathCast)"),
                              self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(self.tr("When will a satellite image your study area?"))
        self.action.toggled.connect(self._toggle)
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToWebMenu(self.tr("&SwathCast"), self.action)

    def _toggle(self, on: bool) -> None:
        if on and self.dock is None:
            from .dock import SwathcastDock  # import late: keeps QGIS start-up fast

            self.dock = SwathcastDock(self.iface, self.iface.mainWindow())
            self.dock.visibilityChanged.connect(self.action.setChecked)
            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
            from .welcome import show_welcome_once

            # after the dock is on screen, so the notice sits over a visible panel
            QTimer.singleShot(0, lambda: show_welcome_once(self.iface.mainWindow(),
                                                           self.dock.build_info_page))
        if self.dock is not None:
            self.dock.setVisible(on)

    def unload(self) -> None:
        if self.dock is not None:
            self.dock.unload()
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None
        if self.action is not None:
            self.iface.removePluginWebMenu(self.tr("&SwathCast"), self.action)
            self.iface.removeToolBarIcon(self.action)
            self.action = None
        if self.translator is not None:
            QCoreApplication.removeTranslator(self.translator)
            self.translator = None
