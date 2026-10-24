# PyInstaller recipe for the Windows build:
#   .venv-build\Scripts\pyinstaller packaging\lazyfold.spec --noconfirm --distpath packaging\output\dist --workpath packaging\output\build
# Produces packaging\output\dist\Lazyfold\ with Lazyfold.exe (app window) and
# LazyfoldCLI.exe (command line), sharing one set of libraries.
from pathlib import Path

ROOT = Path(SPECPATH).parent
PKG = ROOT / "lazyfold"

datas = [(str(PKG / "ui.html"), "lazyfold"), (str(PKG / "ocr.ps1"), "lazyfold")]
# Never bundle the optional TabPFN/PyTorch stack (hundreds of MB); the
# predictor stays a source-install extra.
excludes = ["torch", "tabpfn", "sklearn", "scipy", "pandas", "lightgbm", "matplotlib", "tkinter", "pytest"]
hidden = ["lazyfold.cli", "lazyfold.desktop", "lazyfold.web", "webview.platforms.edgechromium",
          "webview.platforms.winforms", "clr"]


def analysis(script):
    return Analysis([str(ROOT / "packaging" / script)], pathex=[str(ROOT)], datas=datas,
                    hiddenimports=hidden, excludes=excludes, noarchive=False)


gui = analysis("launch_gui.py")
cli = analysis("launch_cli.py")
MERGE((gui, "launch_gui", "Lazyfold"), (cli, "launch_cli", "LazyfoldCLI"))

common = dict(debug=False, strip=False, upx=False, icon=str(ROOT / "packaging" / "lazyfold.ico"),
              version=str(ROOT / "packaging" / "version_info.txt"))
# Splash image shown the instant Lazyfold.exe starts (before Python loads);
# lazyfold.desktop closes it once the app window is ready.
splash = Splash(str(ROOT / "packaging" / "splash.png"), binaries=gui.binaries, datas=gui.datas,
                text_pos=None, always_on_top=False)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, splash, [], exclude_binaries=True, name="Lazyfold", console=False, **common)
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="LazyfoldCLI", console=True, **common)
COLLECT(gui_exe, gui.binaries, gui.datas, splash.binaries, cli_exe, cli.binaries, cli.datas, name="Lazyfold",
        strip=False, upx=False)
