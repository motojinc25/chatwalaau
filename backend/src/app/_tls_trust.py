"""Keep one truststore in the process under the ``corp`` extra (CTR-0035, UDR-0231).

``pip-system-certs`` injects the truststore vendored in pip into ``ssl`` at interpreter start. The
standalone truststore, which ``httpx2`` imports later, would then record that injected class as the
original ``ssl.SSLContext``, and setting ``verify_mode`` during a handshake recurses until
``RecursionError``. Swapping the injection before the standalone truststore is first imported lets
it record the real class, and the OS trust stays in effect.

This module runs from ``app/__init__.py``: it must import only the standard library.
"""

from __future__ import annotations

import importlib
import logging
import ssl
import sys

logger = logging.getLogger(__name__)

_PIP_TRUSTSTORE = "pip._vendor.truststore"


def repair_truststore_injection() -> None:
    """Replace an injection by pip's truststore with the standalone one. Never raises."""
    if not ssl.SSLContext.__module__.startswith(_PIP_TRUSTSTORE):
        return
    if "truststore" in sys.modules:
        logger.warning("truststore was loaded before the corp TLS repair; HTTPS through httpx2 may fail")
        return
    try:
        importlib.import_module(_PIP_TRUSTSTORE).extract_from_ssl()
        importlib.import_module("truststore").inject_into_ssl()
    except Exception:
        logger.warning("The corp TLS repair failed; HTTPS through httpx2 may fail", exc_info=True)
