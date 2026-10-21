"""Console entrypoint with the bare-version fast path.

Imports only ``sys`` and ``_version``. The CLI is imported lazily so that
``deepsearch-axi --version`` never pays for argparse, TOON, auth, or transport.
"""

import sys

from deepsearch_axi._version import VERSION


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 1 and args[0] in ("-v", "-V", "--version"):
        sys.stdout.write(VERSION + "\n")
        sys.stdout.flush()
        return 0
    from deepsearch_axi.cli import main as cli_main

    return cli_main(args)
