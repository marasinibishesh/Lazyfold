# Entry point of SlothFile.exe (windowed app). Kept outside the package so
# PyInstaller sees a plain script; all logic lives in slothfile.desktop.
import sys

from slothfile.desktop import main

if __name__ == "__main__":
    sys.exit(main())
