; Inno Setup script for Lazyfold-Setup.exe (per-user install, no admin rights).
; Build:  "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" packaging\lazyfold.iss
; Expects the PyInstaller output in packaging\output\dist\Lazyfold (see lazyfold.spec).

#define AppName "Lazyfold"
#ifndef AppVersion
  #define AppVersion "0.7.1"
#endif
#ifndef AppVersionNum
  #define AppVersionNum AppVersion
#endif
#define AppPublisher "Bishesh Marasini"
#define AppURL "https://github.com/marasinibishesh/Lazyfold"

[Setup]
AppId={{0380D0A5-9F30-42DD-A8FD-0895864F7A91}
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
SetupIconFile=lazyfold.ico
UninstallDisplayIcon={app}\Lazyfold.exe
UninstallDisplayName={#AppName}
OutputDir=output
OutputBaseFilename=Lazyfold-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
; The running app holds this mutex: Setup and the uninstaller ask the user to close it first.
AppMutex=LazyfoldAppMutex
VersionInfoVersion={#AppVersionNum}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Tidy with Lazyfold"" to the right-click menu of folders"; GroupDescription: "Shortcuts:"
Name: "runalias"; Description: "Open Lazyfold from Windows+R by typing ""lazyfold"""; GroupDescription: "Shortcuts:"

[InstallDelete]
; Lean and Full share an AppId: never leave the other edition's libraries behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "output\dist\Lazyfold\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs uninsrestartdelete
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "third_party\THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Lazyfold.exe"; Comment: "Tidy folders and clean up storage safely"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Lazyfold.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\LazyfoldCLI.exe"; Parameters: "shell-install"; Tasks: contextmenu; Flags: runhidden waituntilterminated; StatusMsg: "Adding the right-click menu entry..."
Filename: "{app}\LazyfoldCLI.exe"; Parameters: "shell-install --no-menu --run-alias"; Tasks: runalias; Flags: runhidden waituntilterminated; StatusMsg: "Adding the Windows+R shortcut..."
Filename: "{app}\Lazyfold.exe"; Description: "Start Lazyfold now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Remove only what Lazyfold added. Your organized files and undo history are never touched.
Filename: "{app}\LazyfoldCLI.exe"; Parameters: "shell-uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "ShellUninstall"
Filename: "{app}\LazyfoldCLI.exe"; Parameters: "schedule clear"; Flags: runhidden waituntilterminated; RunOnceId: "ScheduleClear"

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
    if SuppressibleMsgBox('Lazyfold needs the Microsoft Edge WebView2 Runtime, which is missing on this PC.' + #13#10 + #13#10 +
              'Open the Microsoft download page now? (Install it, then run this setup again.)',
              mbConfirmation, MB_YESNO, IDNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
  end;
end;
