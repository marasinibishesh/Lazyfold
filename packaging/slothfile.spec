# PyInstaller recipe for the Windows build:
#   .venv-build\Scripts\pyinstaller packaging\slothfile.spec --noconfirm --distpath packaging\output\dist --workpath packaging\output\build
# Produces packaging\output\dist\SlothFile\ with SlothFile.exe (app window) and
# SlothFileCLI.exe (command line), sharing one set of libraries.
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "slothfile"

datas = [(str(PKG / "ui.html"), "slothfile"), (str(PKG / "ocr.ps1"), "slothfile")]
# Never bundle the optional TabPFN/PyTorch stack (hundreds of MB); the
# predictor stays a source-install extra.
excludes = ["torch", "tabpfn", "sklearn", "scipy", "pandas", "lightgbm", "matplotlib", "tkinter", "pytest"]
hidden = ["slothfile.cli", "slothfile.desktop", "slothfile.web", "webview.platforms.edgechromium",
          "webview.platforms.winforms", "clr"]


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
