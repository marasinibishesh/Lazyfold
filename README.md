# Lazyfold

**A Windows and Mac app that tidies your messy Downloads folder and frees up disk space — safely, on your own PC.**

Created by **[Bishesh Marasini](https://github.com/marasinibishesh)**.

![Lazyfold review screen: TabPFN lists the moves you'd probably change first (demo folder and demo history)](docs/screenshots/review.png)

- **Organize** — sorts files into folders (Documents, Images, Installers…) and suggests a clear name for each file from what's inside it, for example `Presentation1.pptx` → `Contoso Coffee Q3 2026 Sales Review.pptx`. One click keeps or drops each name. Nothing moves until you approve, and every run can be undone.
- **Clean up** — finds temporary files and caches you can safely remove (the safe version of Windows+R → `%temp%`). Files go to the Recycle Bin (the Trash on a Mac) by default.
- **Automation** — tidy on a schedule, from the right-click menu (a Finder Quick Action on a Mac), or with Windows+R → `lazyfold`.
- **Private** — everything runs on your computer. No account, no upload.

## Partner technology

Lazyfold works like a small agent. **TabPFN predicts** whether *you* would accept each proposed move, learning from your own past reviews. The proposals come from the built-in rules and, if you turn it on, **Gemma**. Lazyfold then **acts or asks you**.

### 1. TabPFN by Prior Labs — predicts which moves you'd accept

**Built with PriorLabs-TabPFN.** TabPFN is a tabular foundation model. It runs on your CPU and learns only from your own review history: a table on your PC with one row per move you reviewed. Each row holds the file type, size, age, download site, the folder Lazyfold proposed, the folder you chose, and whether you accepted. File names and contents are never stored.

Lazyfold uses its prediction in four places:

- **Automatic tidy.** A file moves without asking only if the safe built-in rules are sure *and* TabPFN predicts you'd accept the move (85% by default, set in Settings). Everything else waits for you, and the notification says how many files were held back. TabPFN can only hold a move back. It never adds a move, renames or deletes anything.
- **Review.** The moves you'd probably change are listed first under **Worth a look**. Nothing is unticked for you.
- **Learning.** Your reviews become new training rows. Automatic moves you keep count as accepted, and **undoing an automatic tidy** teaches it to ask first next time.
- **How well it knows you.** *Settings → Smart predictions → Check now* tests TabPFN on your newest reviews, which it was not trained on. It compares the result with simply accepting everything, so you can see whether it's worth trusting.

It starts after 30 reviews, with at least 5 you accepted and 5 you changed. The **Full** installer bundles the TabPFN v2 model, so it works offline. The standard installer leaves TabPFN out and uses the rules alone.

**Privacy:** Lazyfold deliberately doesn't use Prior Labs' cloud API, Thinking Mode or MCP server, because they would upload information about your files.

### 2. Gemma 4 by Google — reads documents (optional)

Gemma runs locally through [Ollama](https://ollama.com) (*Settings → Local AI → Set up*, about 4.3 GB, one time). It reads PDFs, Word documents, slides and spreadsheets, and looks at pictures and screenshots, then suggests a clear name for each (and a folder for documents). Names for documents must use words from the file; picture names keep the date the camera or screenshot gave them. Every name is a suggestion: one click keeps or drops it. Moves Gemma influenced always wait for your review, and TabPFN learns from your decisions how far to trust them.

---

## 1. Install

1. Go to the **[latest release](https://github.com/marasinibishesh/Lazyfold/releases/latest)**.
2. Download one installer:

   | File | Size | Choose it if… |
   |---|---|---|
   | `Lazyfold-Setup-<version>.exe` | ~18 MB | you want the normal app (**recommended**) |
   | `Lazyfold-Setup-Full-<version>.exe` | ~260 MB | you also want *Smart predictions* (TabPFN), bundled to work offline |

3. Run it. If Windows says **"Windows protected your PC"**, click **More info → Run anyway** (the installer is not code-signed).
4. Open **Lazyfold** from the Start menu.

Needs Windows 10 or 11 (64-bit). No administrator rights are needed.

### On a Mac

1. From the same release, download `Lazyfold-<version>-mac.dmg` (~22 MB), or `Lazyfold-Full-<version>-mac.dmg` (~360 MB) for *Smart predictions*. It needs an Apple Silicon Mac (M1 or newer) with macOS 11 or later.
2. Open the .dmg and drag **Lazyfold** onto **Applications**.
3. The first time, **right-click Lazyfold → Open → Open** (the app is not signed or notarized by Apple). If macOS still refuses, go to **System Settings → Privacy & Security** and click **Open Anyway**.

What's different on a Mac:

- Removed files go to the **Trash** instead of the Recycle Bin.
- The right-click menu entry is a **Finder Quick Action**, and the quick shortcut is **Spotlight** (⌘ Space → `Lazyfold`) instead of Windows+R.
- Scheduled tidies run through macOS's own scheduler (launchd).
- To uninstall, drag Lazyfold from Applications to the Trash. Settings and undo history are in `~/Library/Application Support/Lazyfold`.

Intel Macs: [run from source](#run-from-source). The Mac app is built and tested automatically on GitHub's macOS machines.

## 2. Use it

**Organize a folder**

1. Open **Organize**, pick a folder (or click *Downloads*), and click **Scan folder**. Scanning never changes anything.
2. Review the plan. Untick anything you don't want moved; click a **Rename to …** chip to keep a file's current name; click ⓘ to see why a file goes where it goes.
3. Click **Move files**. Changed your mind? Click **Undo** — now, or later from **History**.

![Suggested names: one click keeps or drops each](docs/screenshots/names.png)

**Free up space**

1. Open **Clean up** and click **Scan**.
2. Tick what to remove and click **Clean selected**. Files go to the Recycle Bin (the Trash on a Mac) unless you choose *Delete permanently* (which asks you to confirm).

**Let it work for you**

Open **Automation** to tidy a folder every day, turn on the right-click menu entry (Finder Quick Action on a Mac), or add the Windows+R shortcut. Automatic runs only move the kinds of file you trust (installers, images, archives…). Documents always wait for you.

![Clean up](docs/screenshots/cleanup.png)

## 3. Settings

Open **Settings** (bottom-left) to change the default folder, the theme (System / Light / Dark), and which features are on.

- **Smart predictions (TabPFN)** — in the Full edition. Turn it on or off, choose how careful the automatic tidy is, and click **Check now** to see how well it predicts your choices (see [Partner technology](#partner-technology)).
- **Local AI (Gemma 4)** — optional. Reads documents, slides, spreadsheets and pictures to sort and name them better (*Suggest names from what's inside* can be turned off). Click **Set up** once: it installs [Ollama](https://ollama.com) (on a Mac through Homebrew, or it points you to Ollama's Mac download) and downloads Gemma 4 (about 4.3 GB). It runs entirely on your PC and can only *suggest*; you still approve every move.
- **Privacy & learning** — see or forget what Lazyfold learned from your corrections.

![Settings](docs/screenshots/settings.png)

## 4. Uninstall

**Settings → Apps → Installed apps → Lazyfold → Uninstall.** This removes the app, its shortcuts and its scheduled tasks. Files you organized stay where they are.

Your undo history and settings are kept in `%LOCALAPPDATA%\Lazyfold`; delete that folder too if you want them gone.

On a Mac, drag Lazyfold from Applications to the Trash (see [On a Mac](#on-a-mac)).

## Safety

- Scanning and previewing never change files. Only files directly inside the chosen folder are touched; subfolders are left alone.
- Nothing is ever overwritten. Name clashes get ` (2)`.
- Organizing never deletes. Duplicates are moved to a `Duplicates` folder.
- Every move is written to a journal first, so a run can be undone even after a crash or power cut.
- The AI can only pick from a fixed list of folders. It cannot move, delete or approve anything.
- Permanent delete and emptying the Recycle Bin are always separate, confirmed steps.

---

## For developers

### Run from source

Requires [Python 3.10+](https://www.python.org/downloads/) and Git. In PowerShell:

```powershell
git clone https://github.com/marasinibishesh/Lazyfold.git
cd Lazyfold
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[content,desktop]"

.\.venv\Scripts\lazyfold demo      # makes a messy sample folder to try it on
.\.venv\Scripts\lazyfold desktop   # opens the app window
```

Optional Smart predictions in a source install:

```powershell
.\.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python -m pip install -e ".[predict]"
```

On a Mac (Apple Silicon or Intel), the same in Terminal:

```bash
git clone https://github.com/marasinibishesh/Lazyfold.git
cd Lazyfold
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[content,desktop]"
.venv/bin/lazyfold desktop
```

Everything is also available on the command line — run `.\.venv\Scripts\lazyfold --help`. The installed app includes the same commands as `LazyfoldCLI.exe`.

### Run the tests

```powershell
.\.venv\Scripts\python -m pip install -e ".[content,dev]"
.\.venv\Scripts\python -m pytest -q
```

Tests only use temporary folders.

### Build the installer

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\python -m pip install "pywebview>=6.2" "pyinstaller>=6.22" "pypdf>=6.21.2" "pillow>=12"
.\.venv-build\Scripts\pyinstaller packaging\lazyfold.spec --noconfirm --distpath packaging\output\dist --workpath packaging\output\build
winget install -e --id JRSoftware.InnoSetup --scope user
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\lazyfold.iss
```

The installer appears in `packaging\output\`. On a Mac, `pyinstaller packaging/lazyfold-mac.spec --noconfirm --distpath packaging/output/dist --workpath packaging/output/build` builds `Lazyfold.app`; its command line is `Lazyfold.app/Contents/MacOS/LazyfoldCLI`. GitHub Actions tests on Windows and macOS, builds the Windows installers and the Mac .dmg files, and attaches them to a release whenever a version tag such as `v0.5.0` is pushed (see [`.github/workflows/build.yml`](.github/workflows/build.yml)).

---

## License and credits

**Lazyfold is created by Bishesh Marasini** and released under the [MIT License](LICENSE).

It builds on:
- [TabPFN](https://github.com/PriorLabs/TabPFN) by Prior Labs — **Built with PriorLabs-TabPFN.** The package is Apache 2.0; the v2 weights shipped in the Full edition are under the [Prior Labs License](https://huggingface.co/Prior-Labs/TabPFN-v2-clf/blob/main/LICENSE.txt), and a copy is installed with the app. Source installs that set `TABPFN_TOKEN` (after accepting Prior Labs' non-commercial TabPFN-3.5 license) switch to TabPFN-3.5-Fast (this path is not covered by Lazyfold's tests); Lazyfold never ships the 3.5 weights.
- [Gemma 4](https://ai.google.dev/gemma) by Google, run locally with [Ollama](https://ollama.com) — optional, downloaded on your PC, not included here.
- [pywebview](https://pywebview.flowrl.com), [pypdf](https://github.com/py-pdf/pypdf), [Pillow](https://python-pillow.org), [PyInstaller](https://pyinstaller.org) and [Inno Setup](https://jrsoftware.org/isinfo.php).

The full list of bundled components is in `THIRD-PARTY-NOTICES.txt` in the install folder.
