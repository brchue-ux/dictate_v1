"""Test suite.

Run it with:  python -m unittest discover -s tests -t .
(pytest also collects these; unittest is used so the suite runs with nothing
installed but the standard library, which is how it runs in CI and how it ran on
the machine this was built on.)
"""

import logging

# Several tests deliberately drive error paths. Their log output is expected, and
# printing it makes a passing run look like a failing one.
logging.disable(logging.CRITICAL)
