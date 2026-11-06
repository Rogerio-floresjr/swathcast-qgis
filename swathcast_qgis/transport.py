"""HTTP through QGIS's network stack, so the user's proxy and SSL settings apply."""
from __future__ import annotations

from typing import Optional

from qgis.core import Qgis, QgsBlockingNetworkRequest, QgsFeedback
from qgis.PyQt.QtCore import QByteArray, QUrl
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from .client import NET_FATAL, NET_PROXY, NET_TRANSIENT, ApiError
from .info import plugin_version

USER_AGENT = f"swathcast-qgis/{plugin_version()} QGIS/{Qgis.QGIS_VERSION.split('-')[0]}"

_E = QNetworkReply.NetworkError
#: failures a retry will not fix: say so at once instead of waiting for a "waking" server
_PROXY_ERRORS = {_E.ProxyConnectionRefusedError, _E.ProxyNotFoundError,
                 _E.ProxyAuthenticationRequiredError}
_FATAL_ERRORS = {_E.HostNotFoundError, _E.SslHandshakeFailedError,
                 _E.ProtocolUnknownError, _E.ContentAccessDenied}


def network_status(error) -> int:
    """The client's code for a reply that has no HTTP status."""
    if error in _PROXY_ERRORS:
        return NET_PROXY
    if error in _FATAL_ERRORS:
        return NET_FATAL
    return NET_TRANSIENT  # timeouts, refused or closed connections: a server still booting


class QgisTransport:
    """``client.Transport`` over ``QgsBlockingNetworkRequest``.

    Blocking, so call it from a ``QgsTask`` (a worker thread), never the GUI thread.
    """

    def __init__(self, api_key: str = "", feedback: Optional[QgsFeedback] = None) -> None:
        self.api_key = api_key
        self.feedback = feedback

    def request(self, method: str, url: str, body: Optional[bytes],
                timeout_s: float) -> tuple[int, bytes]:
        req = QNetworkRequest(QUrl(url))
        req.setRawHeader(b"User-Agent", USER_AGENT.encode())
        req.setRawHeader(b"Accept", b"application/json")
        if self.api_key:
            req.setRawHeader(b"X-API-Key", self.api_key.encode())
        req.setTransferTimeout(int(timeout_s * 1000))
        blocking = QgsBlockingNetworkRequest()
        if method == "GET":
            blocking.get(req, True, self.feedback)
        elif method == "POST":
            req.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            blocking.post(req, QByteArray(body or b""), True, self.feedback)
        else:
            raise ValueError(method)
        if self.feedback is not None and self.feedback.isCanceled():
            raise ApiError("canceled", 0)
        reply = blocking.reply()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if not status:
            return network_status(reply.error()), blocking.errorMessage().encode()
        return int(status), bytes(reply.content())
