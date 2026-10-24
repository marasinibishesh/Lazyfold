# PyInstaller recipe for the "Full" macOS build: the lean Mac build
# (lazyfold-mac.spec) plus PyTorch (the PyPI macOS arm64 wheel, CPU/MPS),
# tabpfn and the TabPFN v2 classifier weights, so the approval predictor works
# offline right after install.
#   pyinstaller packaging/lazyfold-mac-full.spec --noconfirm --distpath packaging/output-full/dist --workpath packaging/output-full/build
# The checkpoint is never committed: it is copied from $LAZYFOLD_TABPFN_CKPT
# (what CI sets), or from ~/Library/Caches/tabpfn. Keep its original file name;
# tabpfn infers the model version from it.
# Keep the shared parts in step with lazyfold-mac.spec and lazyfold-full.spec.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Python 3.12.0 itself breaks frozen scipy; 3.12.1+ is fine.
if sys.version_info < (3, 12, 1):
    sys.exit(f"Build the Full edition with Python 3.12.1 or newer (this is {sys.version.split()[0]})")

ROOT = Path(SPECPATH).parent
PKG = ROOT / "lazyfold"
CKPT_NAME = "tabpfn-v2-classifier-finetuned-zk73skhh.ckpt"


def app_version() -> str:
    for line in (PKG / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("lazyfold/__init__.py has no __version__")


VERSION = app_version()
ckpt = Path(os.environ.get("LAZYFOLD_TABPFN_CKPT")
            or Path.home() / "Library" / "Caches" / "tabpfn" / CKPT_NAME)
if not ckpt.is_file():
    sys.exit(f"TabPFN checkpoint not found at {ckpt}. Download it from "
             f"https://huggingface.co/Prior-Labs/TabPFN-v2-clf/resolve/main/{CKPT_NAME} "
             "and set LAZYFOLD_TABPFN_CKPT to its path.")
if not ckpt.name.startswith("tabpfn-v2-classifier"):
    sys.exit(f"{ckpt.name}: keep the original checkpoint file name (tabpfn reads the version from it)")

# In the .app, data files live in Contents/Resources and are symlinked into
# Contents/Frameworks, which is sys._MEIPASS; predict.bundled_checkpoint()
# looks in _MEIPASS/tabpfn_models.
datas = [(str(PKG / "ui.html"), "lazyfold"), (str(PKG / "ocr.ps1"), "lazyfold"),
         (str(ckpt), "tabpfn_models")]
# License and notices go to Lazyfold.app/Contents/Resources; Prior Labs License
# section 10 requires shipping its license with the weights. CI first rewrites
# the notices from the Mac build environment (make_notices.py full).
datas += [(str(ROOT / "LICENSE"), "."),
          (str(ROOT / "packaging" / "third_party" / "THIRD-PARTY-NOTICES-FULL.txt"), "."),
          (str(ROOT / "packaging" / "third_party" / "TabPFN-v2-LICENSE.txt"), ".")]
# tabpfn loads tabpfn/architectures/shared/tabpfn_col_embedding.pt via __file__
# and imports tabpfn.classifier by name; it and skrub read their own versions
# from package metadata at import time.
datas += collect_data_files("tabpfn") + collect_data_files("skrub")
for dist in ("tabpfn", "skrub", "torch", "scikit-learn", "scipy", "numpy", "pandas", "lightgbm",
             "einops", "pydantic", "pydantic-settings", "huggingface-hub", "safetensors"):
    datas += copy_metadata(dist)  # also carries each package's license files
excludes = ["tkinter", "pytest", "wandb", "IPython", "mlx", "tabpfn.visualisation", "torch.utils.tensorboard"]
hidden = ["lazyfold.cli", "lazyfold.desktop", "lazyfold.web", "lazyfold.predict", "lazyfold.macos",
          "webview.platforms.cocoa", "objc", "Foundation", "Vision"]
hidden += collect_submodules("tabpfn", filter=lambda n: not n.startswith(("tabpfn.visualisation",
                                                                          "tabpfn.finetuning")))


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
    # BUNDLE copies `console` from the COLLECT (the last EXE, the console CLI),
    # which would mark the app LSBackgroundOnly. It is a normal windowed app.
    "LSBackgroundOnly": False,
    "NSDownloadsFolderUsageDescription": "Lazyfold organizes the files in your Downloads folder when you ask it to.",
    "NSDesktopFolderUsageDescription": "Lazyfold organizes a Desktop folder when you pick it.",
    "NSDocumentsFolderUsageDescription": "Lazyfold organizes a Documents folder when you pick it.",
    "NSAppleEventsUsageDescription": "Lazyfold asks Finder to move items to the Trash or empty it when you choose to.",
}
# gui_exe first, so CFBundleExecutable is Lazyfold (see lazyfold-mac.spec).
app = BUNDLE(gui_exe, coll, name="Lazyfold.app", icon=str(ROOT / "packaging" / "lazyfold.icns"),
             bundle_identifier="com.bisheshmarasini.lazyfold", version=VERSION, info_plist=INFO_PLIST)
