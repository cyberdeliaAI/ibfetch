"""PyInstaller-only startup hook: give the bundled interpreter portable trusted roots."""

import os
from pathlib import Path
import ssl
import sys


def configure_certificates():
    # Preserve explicit corporate/private trust settings. Never change system settings.
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return
    bundle = Path(sys._MEIPASS) / "certificates" / "cacert.pem"
    if not bundle.is_file():
        raise RuntimeError("The desktop certificate bundle is missing. Reinstall ibfetch.")
    context = ssl.create_default_context(cafile=str(bundle))
    if not context.get_ca_certs():
        raise RuntimeError("The desktop certificate bundle contains no trusted roots.")
    os.environ["SSL_CERT_FILE"] = str(bundle)


configure_certificates()
