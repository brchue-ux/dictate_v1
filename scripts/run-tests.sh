#!/usr/bin/env bash
# Run the whole suite with nothing installed but the standard library.
#
# The core of dictate has no third-party dependencies on purpose - that is what
# lets the pipeline, the cleanup pass, the process supervisor and the HTTP client
# be tested on a machine that is not the Windows box the product runs on.
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=src exec python3 -W ignore::ResourceWarning \
    -m unittest discover -s tests -t . "$@"
