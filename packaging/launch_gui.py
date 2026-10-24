# Entry point of Lazyfold.exe (windowed app). Kept outside the package so
# PyInstaller sees a plain script; all logic lives in lazyfold.desktop.
import sys

from lazyfold.desktop import main

if __name__ == "__main__":
    sys.exit(main())
