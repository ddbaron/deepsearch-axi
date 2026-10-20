"""Load the socket guard in child Pythons when the harness asks for it."""

import os
import sys
from pathlib import Path

if os.environ.get("DEEPSEARCH_AXI_NETGUARD") == "1":
    _support_dir = Path(__file__).resolve().parent.parent
    if str(_support_dir) not in sys.path:
        sys.path.insert(0, str(_support_dir))
    import netguard

    netguard.install()
