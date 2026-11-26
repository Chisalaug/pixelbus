"""让 `python -m pixelbus ...` 和 console_scripts 走同一套入口。"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
