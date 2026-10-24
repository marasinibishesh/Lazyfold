# PyInstaller recipe for the macOS build (run on a Mac, Apple Silicon):
#   pyinstaller packaging/lazyfold-mac.spec --noconfirm --distpath packaging/output/dist --workpath packaging/output/build
# Produces packaging/output/dist/Lazyfold.app with two programs in Contents/MacOS:
#   Lazyfold     the app window (what Finder and the Dock open)
#   LazyfoldCLI  the command line, e.g. Lazyfold.app/Contents/MacOS/LazyfoldCLI plan ~/Downloads
# Both share one set of libraries: the two EXEs go into one COLLECT, and BUNDLE
# wraps that COLLECT, so every EXECUTABLE entry lands in Contents/MacOS.
# Keep the shared parts in step with lazyfold.spec (Windows) and lazyfold-mac-full.spec.
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "lazyfold"


def app_version() -> str:
    for line in (PKG / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("lazyfold/__init__.py has no __version__")


VERSION = app_version()
datas = [(str(PKG / "ui.html"), "lazyfold"), (str(PKG / "ocr.ps1"), "lazyfold")]
# License and notices end up in Lazyfold.app/Contents/Resources. CI first
# rewrites the notices from the Mac build environment (make_notices.py lean).
datas += [(str(ROOT / "LICENSE"), "."), (str(ROOT / "packaging" / "third_party" / "THIRD-PARTY-NOTICES.txt"), ".")]
# Never bundle the optional TabPFN/PyTorch stack (hundreds of MB); the
# predictor stays a source-install extra (or the Full build).
excludes = ["torch", "tabpfn", "sklearn", "scipy", "pandas", "lightgbm", "matplotlib", "tkinter", "pytest"]
# Cocoa/WebKit window instead of WinForms/Edge; Foundation and Vision are only
# imported inside functions by lazyfold.macos (Trash, OCR).
hidden = ["lazyfold.cli", "lazyfold.desktop", "lazyfold.web", "lazyfold.macos", "webview.platforms.cocoa",
          "objc", "Foundation", "Vision"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")
MERGE((gui, "launch_gui", "Lazyfold"), (cli, "launch_cli", "LazyfoldCLI"))

# No Splash: PyInstaller does not support it on macOS.
common = dict(debug=False, strip=False, upx=False, argv_emulation=False, target_arch=None,
              codesign_identity=None, entitlements_file=None)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="Lazyfold", console=False, **common)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="LazyfoldCLI", console=True, **common)
coll = COLLECT(gui_exe, gui.binaries, gui.datas, cli_exe, cli.binaries, cli.datas, name="Lazyfold",
               strip=False, upx=False)

INFO_PLIST = {
    "CFBundleName": "Lazyfold",
    "CFBundleDisplayName": "Lazyfold",
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
    # Shown by macOS the first time Lazyfold touches these places or asks
    # Finder to empty the Trash.
    "NSDownloadsFolderUsageDescription": "Lazyfold organizes the files in your Downloads folder when you ask it to.",
    "NSDesktopFolderUsageDescription": "Lazyfold organizes a Desktop folder when you pick it.",
    "NSDocumentsFolderUsageDescription": "Lazyfold organizes a Documents folder when you pick it.",
    "NSAppleEventsUsageDescription": "Lazyfold asks Finder to move items to the Trash or empty it when you choose to.",
}
# BUNDLE takes the COLLECT (both EXEs). gui_exe is passed first so the bundle's
# CFBundleExecutable is Lazyfold, not LazyfoldCLI, whatever order the
# COLLECT's table of contents ends up in.
app = BUNDLE(gui_exe, coll, name="Lazyfold.app", icon=str(ROOT / "packaging" / "lazyfold.icns"),
             bundle_identifier="com.bisheshmarasini.lazyfold", version=VERSION, info_plist=INFO_PLIST)
