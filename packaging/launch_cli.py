# Entry point of LazyfoldCLI.exe (console): the full command line, e.g.
#   LazyfoldCLI.exe plan "C:\Users\me\Downloads"
# With no arguments it opens the app window, like Lazyfold.exe.
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()  # the file-reading worker re-launches this exe
    from lazyfold.desktop import hold_app_mutex
    if len(sys.argv) > 1 and sys.argv[1] in ("watch", "auto", "ui", "desktop"):
        hold_app_mutex()  # long-running: the uninstaller should ask to close it first
    if len(sys.argv) == 1 or sys.argv[1] in ("ui", "desktop"):
        from lazyfold.desktop import main as desktop_main
        sys.exit(desktop_main(sys.argv[2:] and ["ui", *sys.argv[2:]] or []))
    from lazyfold.cli import main
    sys.exit(main())
