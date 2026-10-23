# PyInstaller recipe for the macOS build (run on a Mac, Apple Silicon):
#   pyinstaller packaging/slothfile-mac.spec --noconfirm --distpath packaging/output/dist --workpath packaging/output/build
# Produces packaging/output/dist/SlothFile.app with two programs in Contents/MacOS:
#   SlothFile     the app window (what Finder and the Dock open)
#   SlothFileCLI  the command line, e.g. SlothFile.app/Contents/MacOS/SlothFileCLI plan ~/Downloads
# Both share one set of libraries: the two EXEs go into one COLLECT, and BUNDLE
# wraps that COLLECT, so every EXECUTABLE entry lands in Contents/MacOS.
# Keep the shared parts in step with slothfile.spec (Windows) and slothfile-mac-full.spec.
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "slothfile"


def app_version() -> str:
    for line in (PKG / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("slothfile/__init__.py has no __version__")


VERSION = app_version()
datas = [(str(PKG / "ui.html"), "slothfile"), (str(PKG / "ocr.ps1"), "slothfile")]
# License and notices end up in SlothFile.app/Contents/Resources. CI first
# rewrites the notices from the Mac build environment (make_notices.py lean).
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "packaging" / "third_party" / "THIRD-PARTY-NOTICES.txt"), ".")]
# Never bundle the optional TabPFN/PyTorch stack (hundreds of MB); the
# predictor stays a source-install extra (or the Full build).
excludes = ["torch", "tabpfn", "sklearn", "scipy", "pandas", "lightgbm", "matplotlib", "tkinter", "pytest"]
# Cocoa/WebKit window instead of WinForms/Edge; Foundation and Vision are only
# imported inside functions by slothfile.macos (Trash, OCR).
hidden = ["slothfile.cli", "slothfile.desktop", "slothfile.web", "slothfile.macos", "webview.platforms.cocoa",
          "objc", "Foundation", "Vision"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")
MERGE((gui, "launch_gui", "SlothFile"), (cli, "launch_cli", "SlothFileCLI"))

# No Splash: PyInstaller does not support it on macOS.
common = dict(debug=False, strip=False, upx=False, argv_emulation=False, target_arch=None,
              codesign_identity=None, entitlements_file=None)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="SlothFile", console=False, **common)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="SlothFileCLI", console=True, **common)
coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name="SlothFile",
               strip=False, upx=False)

INFO_PLIST = {
    "CFBundleName": "SlothFile",
    "CFBundleDisplayName": "SlothFile",
    "CFBundleShortVersionString": VERSION,
    "CFBundleVersion": VERSION,
    "NSHumanReadableCopyright": "© 2026 Bishesh Marasini",
    "LSMinimumSystemVersion": "11.0",
    "LSApplicationCategoryType": "public.app-category.utilities",
    "NSHighResolutionCapable": True,
    # BUNDLE copies `console` from the COLLECT, which takes it from the last
    # EXE passed (the console CLI) and would then mark the app LSBackgroundOnly
    # (no Dock icon, no menu bar). The app is a normal windowed app.
    "LSBackgroundOnly": False,
    # Shown by macOS the first time SlothFile touches these places or asks
    # Finder to empty the Trash.
    "NSDownloadsFolderUsageDescription": "SlothFile organizes the files in your Downloads folder when you ask it to.",
    "NSDesktopFolderUsageDescription": "SlothFile organizes a Desktop folder when you pick it.",
    "NSDocumentsFolderUsageDescription": "SlothFile organizes a Documents folder when you pick it.",
    "NSAppleEventsUsageDescription": "SlothFile asks Finder to move items to the Trash or empty it when you choose to.",
}
# BUNDLE takes the COLLECT (both EXEs). gui_exe is passed first so the bundle's
# CFBundleExecutable is SlothFile, not SlothFileCLI, whatever order the
# COLLECT's table of contents ends up in.
app = BUNDLE(gui_exe, coll, name="SlothFile.app", icon=str(ROOT / "packaging" / "slothfile.icns"),
             bundle_identifier="com.bisheshmarasini.slothfile", version=VERSION, info_plist=INFO_PLIST)
