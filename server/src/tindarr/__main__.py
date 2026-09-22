"""Allow ``python -m tindarr``."""

import sys

from tindarr.main.cli import main

sys.exit(main())
