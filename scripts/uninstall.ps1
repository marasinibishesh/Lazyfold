<#
.SYNOPSIS
    Removes SlothFile's program files, shortcuts and scheduled tasks.

.DESCRIPTION
    Never touches the files SlothFile organized. Your undo journals and the
    Ollama model are kept unless you say yes to their own separate questions
    (the default answer is no for both).

.PARAMETER Yes
    Accept the default answer to every question: removes the program, keeps
    journals, keeps the model and Ollama.
.PARAMETER DryRun
    Only print what would happen; nothing is removed and no questions are asked.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Model      = 'gemma4:e2b-it-qat'
$StateDir   = Join-Path $env:LOCALAPPDATA 'SlothFile'
$AppDir     = Join-Path $StateDir 'app'
$VenvPy     = Join-Path $AppDir '.venv\Scripts\python.exe'
$ToolsRoot  = Join-Path $env:LOCALAPPDATA 'SlothFile-tools'
$ToolsDir   = Join-Path $ToolsRoot 'ollama'
$Shortcut   = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\SlothFile.lnk'
$AppPathKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\App Paths\slothfile.exe'
$TaskFolder = '\SlothFile\'
# Undo history SlothFile wrote. Only removed after its own explicit "yes".
$HistoryDirs = @('runs', 'plans', 'cleanup')

function Say([string]$msg)  { Write-Host "  $msg" }
function Step([string]$msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Warn([string]$msg) { Write-Host "  ! $msg" -ForegroundColor Yellow }

function Confirm-Action([string]$question, [bool]$default) {
    if ($DryRun) { Say "[dry-run] would ask: $question (default: $(if ($default) {'yes'} else {'no'}))"; return $true }
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
    if ($DryRun) { Say "[dry-run] $description"; return }
    Say $description
    & $action | Out-Host
}

function Test-Reparse([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    return ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))
}

function Remove-OwnedFolder([string]$path, [string]$mustEndWith) {
    # Defence in depth before any recursive delete: the path must be exactly
    # the folder we created under LOCALAPPDATA and must not be a junction or
    # symlink (Remove-Item -Recurse in 5.1 can follow a junction into its
    # target). rmdir /s removes nested junctions without entering them.
    $full = [IO.Path]::GetFullPath($path).TrimEnd('\')
    $base = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
    # cmd.exe treats % ! ^ " as special. Even though the path is handed over
    # through an environment variable below (cmd expands that only once), a
    # path containing them is not one we created, so refuse it outright.
    if ($full.IndexOfAny([char[]]'%!^"') -ge 0) {
        Warn "Refusing to delete $full (unexpected characters in the path)"; return
    }
    if (-not $full.EndsWith($mustEndWith, [StringComparison]::OrdinalIgnoreCase) -or
        -not $full.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        Warn "Refusing to delete unexpected path $full"; return
    }
    if (-not (Test-Path -LiteralPath $full)) { Say "$full is already gone."; return }
    if (Test-Reparse $full) { Warn "$full is a link, not a real folder; leaving it alone."; return }
    # The path never appears on cmd's command line: it travels in an env var,
    # and cmd does not re-expand %...% found inside a variable's value.
    # /v:off disables delayed expansion even if the registry turns it on.
    $env:SLOTHFILE_RMDIR = $full
    try {
        if (Test-Reparse $full) { Warn "$full became a link; leaving it alone."; return }
        & cmd.exe /d /v:off /c 'rmdir /s /q "%SLOTHFILE_RMDIR%"'
    } finally { Remove-Item Env:\SLOTHFILE_RMDIR -ErrorAction SilentlyContinue }
    if (Test-Path -LiteralPath $full) { Warn "Some files in $full could not be removed (is SlothFile or Ollama still running?)." }
    else { Say "Removed $full" }
}

Write-Host 'SlothFile uninstaller' -ForegroundColor Green
if ($DryRun) { Write-Host 'DRY RUN: nothing will be removed.' -ForegroundColor Yellow }
Say 'Your organized files are never touched by this script.'

# ---------------------------------------------------------------- shortcut
Step 'Start Menu shortcut'
if (Test-Path -LiteralPath $Shortcut) {
    Invoke-Action "Remove $Shortcut" { Remove-Item -LiteralPath $Shortcut -Force }
} else { Say 'No shortcut found.' }

if (Test-Path -LiteralPath $AppPathKey) {
    Invoke-Action "Remove Run-box entry $AppPathKey" { Remove-Item -LiteralPath $AppPathKey -Recurse -Force }
}

# ---------------------------------------------------------------- Explorer menu
Step 'Explorer right-click menu'
if (Test-Path -LiteralPath $VenvPy) {
    Invoke-Action "$VenvPy -m slothfile shell-uninstall" {
        & $VenvPy -m slothfile shell-uninstall
        if ($LASTEXITCODE -ne 0) { Warn 'shell-uninstall reported a problem (the entry may not have been installed).' }
    }
} else {
    Say 'SlothFile program not found, so the menu entry cannot be removed through it.'
}

# ---------------------------------------------------------------- scheduled tasks
Step "Scheduled tasks in $TaskFolder"
$tasks = @()
$oldEap = $ErrorActionPreference
try {
    # Read-only query; only names inside \SlothFile\ are ever deleted.
    # 'Continue' so a harmless stderr line (e.g. one unreadable task) is not
    # promoted to a terminating error by 5.1 under 'Stop'.
    $ErrorActionPreference = 'Continue'
    $raw = & schtasks.exe /Query /FO CSV /NH 2>$null
    $ErrorActionPreference = $oldEap
    if ($LASTEXITCODE -eq 0 -and $raw) {
        $tasks = @($raw | ConvertFrom-Csv -Header 'TaskName', 'Next', 'Status' |
            ForEach-Object { $_.TaskName } |
            Where-Object { $_ -and $_.StartsWith($TaskFolder, [StringComparison]::OrdinalIgnoreCase) } |
            Sort-Object -Unique)
    }
} catch { Warn "Could not list scheduled tasks: $($_.Exception.Message)" }
finally { $ErrorActionPreference = $oldEap }
if ($tasks.Count -eq 0) { Say 'No SlothFile scheduled tasks found.' }
foreach ($t in $tasks) {
    Invoke-Action "schtasks /Delete /TN `"$t`" /F" {
        & schtasks.exe /Delete /TN $t /F
        if ($LASTEXITCODE -ne 0) { Warn "Could not delete task $t" }
    }
}

# ---------------------------------------------------------------- program files
Step 'Program files'
if (Test-Path -LiteralPath $AppDir) {
    if (Confirm-Action "Delete the SlothFile program folder $AppDir ?" $true) {
        Invoke-Action "Delete $AppDir" { Remove-OwnedFolder $AppDir '\SlothFile\app' }
    } else { Say 'Kept the program folder.' }
} else { Say 'Program folder not found.' }

# ---------------------------------------------------------------- journals (separate, default no)
Step 'Undo history (journals)'
$history = @($HistoryDirs | ForEach-Object { Join-Path $StateDir $_ } | Where-Object { Test-Path -LiteralPath $_ })
if ($history.Count -gt 0) {
    Say 'These journals are what lets SlothFile undo past runs. Without them, past runs cannot be undone.'
    if (Confirm-Action "Also delete the undo history in $StateDir ($($HistoryDirs -join ', '))?" $false) {
        foreach ($h in $history) {
            Invoke-Action "Delete $h" { Remove-OwnedFolder $h ('\SlothFile\' + (Split-Path -Leaf $h)) }
        }
    } else { Say "Kept the undo history in $StateDir" }
} else { Say 'No journals found.' }

# ---------------------------------------------------------------- model + Ollama (separate, default no)
Step 'Local AI model'
$ollama = $null
$cmd = Get-Command ollama -ErrorAction SilentlyContinue
if ($cmd) { $ollama = $cmd.Source }
elseif (Test-Path -LiteralPath (Join-Path $ToolsDir 'ollama.exe')) { $ollama = Join-Path $ToolsDir 'ollama.exe' }
if ($ollama) {
    # Only SlothFile's model is offered; other Ollama models belong to you.
    if (Confirm-Action "Delete the model $Model from Ollama (other models are kept)?" $false) {
        Invoke-Action "ollama rm $Model" {
            & $ollama rm $Model
            if ($LASTEXITCODE -ne 0) { Warn "ollama rm failed (is Ollama running? is the model installed?)" }
        }
    } else { Say "Kept the model $Model." }
} else { Say 'Ollama not found; nothing to do for the model.' }

if (Test-Path -LiteralPath $ToolsDir) {
    if (Confirm-Action "Delete the portable Ollama program SlothFile installed in $ToolsDir ?" $false) {
        Invoke-Action "Stop Ollama processes started from $ToolsDir, then delete it" {
            Get-Process -Name 'ollama*' -ErrorAction SilentlyContinue |
                Where-Object { $_.Path -and $_.Path.StartsWith($ToolsDir + '\', [StringComparison]::OrdinalIgnoreCase) } |
                Stop-Process -Force -ErrorAction SilentlyContinue
            Remove-OwnedFolder $ToolsDir '\SlothFile-tools\ollama'
            if ((Test-Path -LiteralPath $ToolsRoot) -and -not (Get-ChildItem -LiteralPath $ToolsRoot -Force)) {
                Remove-Item -LiteralPath $ToolsRoot -Force
            }
        }
    } else { Say 'Kept the portable Ollama program.' }
}

# ---------------------------------------------------------------- done
Step 'Summary'
if ($DryRun) { Say 'Dry run finished. Nothing was removed.' }
else {
    Say 'SlothFile is uninstalled. Your organized files were not touched.'
    if ((Test-Path -LiteralPath $StateDir) -and (Get-ChildItem -LiteralPath $StateDir -Force)) {
        Say "Remaining SlothFile data (journals, settings) is in $StateDir"
    }
}
