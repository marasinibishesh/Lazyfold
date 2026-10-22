<#
.SYNOPSIS
    One-click, per-user installer for SlothFile (no admin rights needed).

.DESCRIPTION
    Installs SlothFile into %LOCALAPPDATA%\SlothFile\app (its own virtual
    environment), optionally sets up Ollama and the local Gemma model, adds a
    Start Menu shortcut and, if you agree, the Explorer right-click entry
    "Tidy with SlothFile".

    Safe to run again: every step checks what is already there first.
    Anything that downloads or installs asks before it happens.
    It never touches the files SlothFile organizes.

.PARAMETER Yes
    Accept the default answer to every question (non-interactive).
.PARAMETER NoModel
    Skip Ollama and the Gemma model entirely.
.PARAMETER DryRun
    Only print what would happen. Nothing is downloaded, installed or changed,
    and no questions are asked.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$NoModel,
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 defaults to TLS 1.0, which GitHub refuses.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Model      = 'gemma4:e2b-it-qat'
$RepoDir    = Split-Path -Parent $PSScriptRoot
$AppDir     = Join-Path $env:LOCALAPPDATA 'SlothFile\app'
$VenvDir    = Join-Path $AppDir '.venv'
$VenvPy     = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPyw    = Join-Path $VenvDir 'Scripts\pythonw.exe'
$ToolsDir   = Join-Path $env:LOCALAPPDATA 'SlothFile-tools\ollama'
$StartMenu  = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$Shortcut   = Join-Path $StartMenu 'SlothFile.lnk'
$AppPathKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\slothfile.exe'
$OllamaUrl  = 'http://127.0.0.1:11434'

$Done = New-Object System.Collections.Generic.List[string]

function Say([string]$msg)  { Write-Host "  $msg" }
function Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn([string]$msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function Confirm-Action([string]$question, [bool]$default = $true) {
    # Dry runs never block on input: they show the question and assume yes,
    # so the full list of actions is printed.
    if ($DryRun) { Say "[dry-run] would ask: $question"; return $true }
    if ($Yes) { return $default }
    $hint = if ($default) { '[Y/n]' } else { '[y/N]' }
    while ($true) {
        $a = Read-Host "  $question $hint"
        if ([string]::IsNullOrWhiteSpace($a)) { return $default }
        switch -Regex ($a.Trim()) {
            '^(y|yes)$' { return $true }
            '^(n|no)$'  { return $false }
        }
        Say 'Please answer y or n.'
    }
}

function Invoke-Action([string]$description, [scriptblock]$action) {
    # Single choke point for every change, so -DryRun cannot miss one.
    if ($DryRun) { Say "[dry-run] $description"; return $null }
    Say $description
    # Out-Host streams pip/winget/ollama progress to the console as it happens
    # instead of letting it leak into the caller's pipeline.
    & $action | Out-Host
}

function Get-PyVersion([string]$exe, [string[]]$prefix) {
    # Returns @{Exe=...; Version=[version]} or $null. Swallows the Microsoft
    # Store "python.exe" alias, which prints nothing useful and exits non-zero.
    # 'Continue' locally: under the script-wide 'Stop', Windows PowerShell 5.1
    # turns any stderr line from a native exe (a warning, PYTHONWARNINGS noise)
    # into a terminating error even with 2>$null, hiding a working Python.
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @prefix -c "import sys; print('%d.%d.%d|%s' % (sys.version_info[:3] + (sys.executable,)))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
        $parts = ([string]($out | Select-Object -First 1)).Trim().Split('|')
        return @{ Version = [version]$parts[0]; Exe = $parts[1] }
    } catch { return $null }
}

function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @('py', @('-3')) }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @('python', @()) }
    foreach ($c in $candidates) {
        $v = Get-PyVersion $c[0] $c[1]
        if ($v -and $v.Version -ge [version]'3.10') { return $v }
        if ($v) { Say ("Found Python {0} at {1}, but SlothFile needs 3.10 or newer." -f $v.Version, $v.Exe) }
    }
    return $null
}

function Test-OllamaUp {
    try { Invoke-RestMethod -Uri "$OllamaUrl/api/version" -TimeoutSec 2 | Out-Null; return $true }
    catch { return $false }
}

function Find-Ollama {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    foreach ($p in @((Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'),
                     (Join-Path $ToolsDir 'ollama.exe'))) {
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
    }
    return $null
}

function Format-Size([double]$bytes) {
    if ($bytes -ge 1GB) { return ('{0:N1} GB' -f ($bytes / 1GB)) }
    return ('{0:N0} MB' -f ($bytes / 1MB))
}

Write-Host "SlothFile installer" -ForegroundColor Green
if ($DryRun) { Write-Host "DRY RUN: nothing will be downloaded, installed or changed." -ForegroundColor Yellow }
Say "Source folder : $RepoDir"
Say "Install folder: $AppDir"

if (-not (Test-Path -LiteralPath (Join-Path $RepoDir 'pyproject.toml'))) {
    throw "Cannot find pyproject.toml next to this script's folder ($RepoDir). Run install.ps1 from inside the SlothFile folder."
}

# ---------------------------------------------------------------- 1. Python
Step 'Looking for Python 3.10 or newer'
$py = Find-Python
if ($py) {
    Say ("Using Python {0} at {1}" -f $py.Version, $py.Exe)
} else {
    Warn 'Python 3.10+ was not found. SlothFile is a Python program, so it needs Python to run.'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Warn 'winget is not available either. Please install Python 3.12 from https://www.python.org/downloads/windows/'
        Warn '(tick "Add python.exe to PATH"), then run this installer again.'
        exit 1
    }
    if (-not (Confirm-Action 'Install Python 3.12 for your user account with winget (about 30 MB download)?')) {
        Warn 'Python is required. Install it from https://www.python.org/downloads/windows/ and run this installer again.'
        exit 1
    }
    Invoke-Action 'winget install -e --id Python.Python.3.12 --scope user' {
        & winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget failed with exit code $LASTEXITCODE" }
    } | Out-Null
    if ($DryRun) {
        $py = @{ Version = [version]'3.12.0'; Exe = '<python installed by winget>' }
    } else {
        # winget updates PATH for new shells only; look in the usual per-user spot too.
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' + $env:Path
        $py = Find-Python
        if (-not $py) {
            $guess = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
            if (Test-Path -LiteralPath $guess) { $py = Get-PyVersion $guess @() }
        }
        if (-not $py) { Warn 'Python was installed, but this window cannot see it yet. Open a new PowerShell window and run the installer again.'; exit 1 }
    }
    $Done.Add("Python $($py.Version) (winget, per user)")
}

# ---------------------------------------------------------------- 2. SlothFile
Step 'Installing SlothFile'
$venvOk = (Test-Path -LiteralPath $VenvPy) -and ((Get-PyVersion $VenvPy @()) -ne $null)
if ($venvOk) {
    Say "Reusing the existing environment in $VenvDir"
} else {
    Invoke-Action "Create folder $AppDir" {
        New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    } | Out-Null
    Invoke-Action "Create a private Python environment in $VenvDir" {
        if (Test-Path -LiteralPath $VenvDir) {
            # A broken venv (e.g. its base Python was removed): rebuild in place.
            & $py.Exe -m venv --clear $VenvDir
        } else {
            & $py.Exe -m venv $VenvDir
        }
        if ($LASTEXITCODE -ne 0) { throw "Creating the virtual environment failed (exit $LASTEXITCODE)" }
    } | Out-Null
}
$spec = '{0}[content]' -f $RepoDir
Invoke-Action 'Update pip (quiet)' {
    & $VenvPy -m pip install --quiet --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { Warn 'Could not update pip; continuing with the bundled one.' }
} | Out-Null
Invoke-Action "pip install `"$spec`" (copies SlothFile, plus pypdf and Pillow for reading documents)" {
    & $VenvPy -m pip install --disable-pip-version-check --upgrade $spec
    if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
    # Same version number as last time would be skipped by pip; force a fresh
    # copy of SlothFile itself (not its dependencies) so re-running updates it.
    & $VenvPy -m pip install --disable-pip-version-check --quiet --force-reinstall --no-deps $RepoDir
    if ($LASTEXITCODE -ne 0) { throw "pip reinstall failed (exit $LASTEXITCODE)" }
} | Out-Null
$Done.Add("SlothFile in $AppDir")

# ---------------------------------------------------------------- 3. Ollama + Gemma
$ollama = $null
if ($NoModel) {
    Step 'Local AI (skipped because of -NoModel)'
} else {
    Step 'Optional local AI (Ollama + Gemma)'
    Say 'Gemma reads documents on your own computer to file and name them better.'
    Say 'Nothing leaves your machine. SlothFile works without it, just less accurately for documents.'
    if (Confirm-Action "Set up the local AI model ($Model)?" $true) {
        $ollama = Find-Ollama
        if ($ollama) {
            Say "Using Ollama at $ollama"
        } elseif ($DryRun) {
            Say "[dry-run] would query https://api.github.com/repos/ollama/ollama/releases/latest for ollama-windows-amd64.zip"
            Say "[dry-run] would ask before downloading it and unpack it to $ToolsDir (portable, no installer)"
            $ollama = Join-Path $ToolsDir 'ollama.exe'
        } else {
            Say 'Ollama is not installed. Looking up the latest portable release on GitHub...'
            $asset = $null
            try {
                $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/ollama/ollama/releases/latest' -Headers @{ 'User-Agent' = 'SlothFile-installer' } -TimeoutSec 30
                $asset = $rel.assets | Where-Object { $_.name -eq 'ollama-windows-amd64.zip' } | Select-Object -First 1
            } catch { Warn "Could not reach GitHub: $($_.Exception.Message)" }
            if (-not $asset) {
                Warn 'Could not find ollama-windows-amd64.zip. Install Ollama from https://ollama.com/download and run this installer again.'
            } elseif (Confirm-Action ("Download Ollama {0} ({1}) to {2}?" -f $rel.tag_name, (Format-Size $asset.size), $ToolsDir) $true) {
                $zip = Join-Path ([IO.Path]::GetTempPath()) ("ollama-{0}.zip" -f [guid]::NewGuid().ToString('N'))
                try {
                    Say 'Downloading (this can take a few minutes)...'
                    $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'  # the 5.1 progress bar slows downloads 10x
                    try { Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing -Headers @{ 'User-Agent' = 'SlothFile-installer' } }
                    finally { $ProgressPreference = $old }
                    $digestProp = $asset.PSObject.Properties['digest']
                    if ($digestProp -and $digestProp.Value -match '^sha256:([0-9a-fA-F]{64})$') {
                        $want = $Matches[1].ToLower()
                        $got = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
                        if ($got -ne $want) { throw "Checksum mismatch for the Ollama download (expected $want, got $got)" }
                        Say 'Checksum verified.'
                    } else {
                        Warn 'GitHub did not publish a checksum for this file; skipping verification.'
                    }
                    Say "Unpacking to $ToolsDir"
                    New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
                    Expand-Archive -LiteralPath $zip -DestinationPath $ToolsDir -Force
                } finally {
                    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
                }
                $ollama = Join-Path $ToolsDir 'ollama.exe'
                if (-not (Test-Path -LiteralPath $ollama)) { Warn 'ollama.exe was not in the archive.'; $ollama = $null }
                else { $Done.Add("Ollama (portable) in $ToolsDir") }
            }
        }

        if ($ollama) {
            if ($DryRun) {
                Say "[dry-run] would start `"$ollama serve`" hidden if it is not already running"
            } elseif (-not (Test-OllamaUp)) {
                Say 'Starting Ollama in the background...'
                Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden | Out-Null
                for ($i = 0; $i -lt 30 -and -not (Test-OllamaUp); $i++) { Start-Sleep -Seconds 1 }
                if (-not (Test-OllamaUp)) { Warn 'Ollama did not start in time; the model download is skipped.' }
            }
            $have = $false
            if (-not $DryRun -and (Test-OllamaUp)) {
                try {
                    $tags = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 5
                    $have = [bool]($tags.models | Where-Object { $_.name -eq $Model })
                } catch { $have = $false }
            }
            if ($have) {
                Say "The model $Model is already downloaded."
            } elseif ($DryRun -or (Test-OllamaUp)) {
                if (Confirm-Action "Download the model $Model (a few GB, one time)?" $true) {
                    Invoke-Action "ollama pull $Model" {
                        & $ollama pull $Model
                        if ($LASTEXITCODE -ne 0) { Warn "ollama pull failed (exit $LASTEXITCODE). You can retry later with: `"$ollama`" pull $Model" }
                        else { $Done.Add("Model $Model (stored by Ollama in $env:USERPROFILE\.ollama)") }
                    } | Out-Null
                }
            }
            Invoke-Action "Save ollama_path = $ollama in SlothFile settings" {
                # Path goes through an environment variable, not the command
                # line: PowerShell 5.1 mangles quotes passed to native programs.
                $env:SLOTHFILE_OLLAMA_PATH = $ollama
                try {
                    & $VenvPy -c "import os; from slothfile.settings import load_settings, save_settings; s = load_settings(); s['ollama_path'] = os.environ['SLOTHFILE_OLLAMA_PATH']; save_settings(s)"
                    if ($LASTEXITCODE -ne 0) { Warn 'Could not save the Ollama path in settings; SlothFile will look for ollama on PATH instead.' }
                } finally { Remove-Item Env:\SLOTHFILE_OLLAMA_PATH -ErrorAction SilentlyContinue }
            } | Out-Null
        }
    }
}

# ---------------------------------------------------------------- 4. Start Menu
Step 'Start Menu shortcut'
Invoke-Action "Create `"$Shortcut`" -> pythonw -m slothfile ui" {
    New-Item -ItemType Directory -Force -Path $StartMenu | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($Shortcut)
    $lnk.TargetPath = $VenvPyw
    $lnk.Arguments = '-m slothfile ui'
    $lnk.WorkingDirectory = $AppDir
    $lnk.Description = 'SlothFile - tidy your Downloads safely'
    $lnk.Save()
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
} | Out-Null
$Done.Add("Start Menu shortcut: $Shortcut")

# Win+R "slothfile": an App Paths entry under HKCU (no admin, no PATH change).
# SlothFile registers it itself (it knows its own windowless launcher), so the
# installer only asks and delegates.
if (Confirm-Action 'Let you start SlothFile by typing "slothfile" in the Run box (Windows+R)?' $true) {
    Invoke-Action "$VenvPy -m slothfile shell-install --run-alias" {
        & $VenvPy -m slothfile shell-install --run-alias
        if ($LASTEXITCODE -ne 0) { Warn 'Could not register the Run box entry (this SlothFile version may not support it yet).' }
        else { $Done.Add('Run box (Windows+R): type slothfile') }
    } | Out-Null
}

# ---------------------------------------------------------------- 5. Explorer menu
Step 'Explorer right-click menu'
if (Confirm-Action 'Add "Tidy with SlothFile" to the right-click menu of folders?' $true) {
    Invoke-Action "$VenvPy -m slothfile shell-install" {
        & $VenvPy -m slothfile shell-install
        if ($LASTEXITCODE -ne 0) { Warn 'Could not add the menu entry (this SlothFile version may not support it yet).' }
        else { $Done.Add('Explorer menu entry "Tidy with SlothFile"') }
    } | Out-Null
}

# ---------------------------------------------------------------- 6. Summary
Step 'Summary'
if ($DryRun) {
    Say 'Dry run finished. Nothing was changed. Run again without -DryRun to install.'
} else {
    foreach ($d in $Done) { Say "+ $d" }
    Say ''
    Say 'Start SlothFile from the Start Menu ("SlothFile"), or run:'
    Say "  `"$VenvPy`" -m slothfile ui"
    Say 'Your undo journals live in %LOCALAPPDATA%\SlothFile\runs.'
}
Say 'To uninstall: powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1'
