; Inno Setup script for SlothFile-Setup.exe (per-user install, no admin rights).
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\slothfile.iss
; Expects the PyInstaller output in packaging\output\dist\SlothFile (see slothfile.spec).

#define AppName "SlothFile"
#ifndef AppVersion
  #define AppVersion "0.3.0"
#endif
#ifndef AppVersionNum
  #define AppVersionNum AppVersion
#endif
#define AppPublisher "Bishesh Marasini"
#define AppURL "https://github.com/marasinibishesh/SlothFile"

[Setup]
AppId={{6A1F3B2C-5D4E-4F80-9A1B-2C3D4E5F6071}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
AppCopyright=Copyright (c) 2026 {#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
LicenseFile=..\LICENSE
SetupIconFile=slothfile.ico
UninstallDisplayIcon={app}\SlothFile.exe
UninstallDisplayName={#AppName}
OutputDir=output
OutputBaseFilename=SlothFile-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
; The running app holds this mutex: Setup and the uninstaller ask the user to close it first.
AppMutex=SlothFileAppMutex
VersionInfoVersion={#AppVersionNum}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Tidy with SlothFile"" to the right-click menu of folders"; GroupDescription: "Shortcuts:"
Name: "runalias"; Description: "Open SlothFile from Windows+R by typing ""slothfile"""; GroupDescription: "Shortcuts:"

[InstallDelete]
; Lean and Full share an AppId: never leave the other edition's libraries behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "output\dist\SlothFile\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs uninsrestartdelete
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "third_party\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\SlothFile.exe"; Comment: "Tidy folders and clean up storage safely"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\SlothFile.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SlothFileCLI.exe"; Parameters: "shell-install"; Tasks: contextmenu; Flags: runhidden waituntilterminated; StatusMsg: "Adding the right-click menu entry..."
Filename: "{app}\SlothFileCLI.exe"; Parameters: "shell-install --no-menu --run-alias"; Tasks: runalias; Flags: runhidden waituntilterminated; StatusMsg: "Adding the Windows+R shortcut..."
Filename: "{app}\SlothFile.exe"; Description: "Start SlothFile now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove only what SlothFile added. Your organized files and undo history are never touched.
Filename: "{app}\SlothFileCLI.exe"; Parameters: "shell-uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "ShellUninstall"
Filename: "{app}\SlothFileCLI.exe"; Parameters: "schedule clear"; Flags: runhidden waituntilterminated; RunOnceId: "ScheduleClear"

[Code]
// The app window uses Microsoft Edge WebView2 (built into Windows 11 and current Windows 10).
function WebView2Installed(): Boolean;
var
  Version: String;
begin
  Result :=
    (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0'));
end;

function InitializeSetup(): Boolean;
var
  ErrorCode: Integer;
begin
  Result := True;
  if not WebView2Installed() then
  begin
    Log('Microsoft Edge WebView2 Runtime is missing; aborting setup.');
    Result := False;
    // Silent installs must never wait on a dialog nobody can see.
    if WizardSilent() then
      Exit;
    if SuppressibleMsgBox('SlothFile needs the Microsoft Edge WebView2 Runtime, which is missing on this PC.' + #13#10 + #13#10 +
              'Open the Microsoft download page now? (Install it, then run this setup again.)',
              mbConfirmation, MB_YESNO, IDNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
