"""Allow ``python -m tindeerr``."""

import sys

from tindeerr.main.cli import main

sys.exit(main())
