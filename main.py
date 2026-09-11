#!/usr/bin/env python3
"""FsTransfor 入口."""
import sys
import warnings

warnings.filterwarnings("ignore")

from fsapp.application import Application


def main() -> int:
    return Application().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
