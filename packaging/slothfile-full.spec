# PyInstaller recipe for the "Full" Windows build: the lean build (slothfile.spec)
# plus CPU PyTorch, tabpfn and the TabPFN v2 classifier weights, so the approval
# predictor works for installed users with no download or network.
#   .venv-build-full\Scripts\pyinstaller packaging\slothfile-full.spec --noconfirm --distpath packaging\output-full\dist --workpath packaging\output-full\build
# The checkpoint is never committed: it is copied from %SLOTHFILE_TABPFN_CKPT%, or
# from %APPDATA%\tabpfn where tabpfn caches it after a first download. Keep its
# original file name; tabpfn infers the model version from it.
# Keep the shared parts in step with slothfile.spec.
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

# Python 3.12.0 itself breaks frozen scipy ("NameError: name 'obj' is not
# defined" in scipy.stats._distn_infrastructure); 3.12.1+ is fine.
if sys.version_info < (3, 12, 1):
    sys.exit(f"Build the Full edition with Python 3.12.1 or newer (this is {sys.version.split()[0]})")

ROOT = Path(SPECPATH).parent
PKG = ROOT / "slothfile"
CKPT_NAME = "tabpfn-v2-classifier-finetuned-zk73skhh.ckpt"

ckpt = Path(os.environ.get("SLOTHFILE_TABPFN_CKPT")
            or Path(os.environ.get("APPDATA", Path.home())) / "tabpfn" / CKPT_NAME)
if not ckpt.is_file():
    sys.exit(f"TabPFN checkpoint not found at {ckpt}. Download it from "
             f"https://huggingface.co/Prior-Labs/TabPFN-v2-clf/resolve/main/{CKPT_NAME} "
             "and set SLOTHFILE_TABPFN_CKPT to its path.")
if not ckpt.name.startswith("tabpfn-v2-classifier"):
    sys.exit(f"{ckpt.name}: keep the original checkpoint file name (tabpfn reads the version from it)")

datas = [(str(PKG / "ui.html"), "slothfile"), (str(PKG / "ocr.ps1"), "slothfile"),
         (str(ckpt), "tabpfn_models")]  # predict.bundled_checkpoint() looks here
# tabpfn loads tabpfn/architectures/shared/tabpfn_col_embedding.pt via __file__
# and imports tabpfn.classifier by name; it and skrub read their own versions
# from package metadata at import time.
datas += collect_data_files("tabpfn") + collect_data_files("skrub")
for dist in ("tabpfn", "skrub", "torch", "scikit-learn", "scipy", "numpy", "pandas", "lightgbm",
             "einops", "pydantic", "pydantic-settings", "huggingface-hub", "safetensors"):
    datas += copy_metadata(dist)  # also carries each package's license files
excludes = ["tkinter", "pytest", "wandb", "IPython", "mlx", "tabpfn.visualisation", "torch.utils.tensorboard"]
hidden = ["slothfile.cli", "slothfile.desktop", "slothfile.web", "slothfile.predict",
          "webview.platforms.edgechromium", "webview.platforms.winforms", "clr"]
hidden += collect_submodules("tabpfn", filter=lambda n: not n.startswith(("tabpfn.visualisation",
                                                                          "tabpfn.finetuning")))


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")
MERGE((gui, "launch_gui", "SlothFile"), (cli, "launch_cli", "SlothFileCLI"))

common = dict(debug=False, strip=False, upx=False, icon=str(ROOT / "packaging" / "slothfile.ico"),
              version=str(ROOT / "packaging" / "version_info.txt"))
# Splash image shown the instant SlothFile.exe starts (before Python loads);
# slothfile.desktop closes it once the app window is ready.
splash = Splash(str(ROOT / "packaging" / "splash.png"), binaries=gui.binaries, datas=gui.datas,
                text_pos=None, always_on_top=False)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, splash, [], exclude_binaries=True, name="SlothFile", console=False, **common)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="SlothFileCLI", console=True, **common)
COLLECT(gui_exe, gui.binaries, gui.datas, splash.binaries, cli_exe, cli.binaries, cli.datas, name="SlothFile",
        strip=False, upx=False)
