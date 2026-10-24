; Lowtide installer (Inno Setup 6). Built by build.ps1, which passes /DAppVersion.
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId=Lowtide
AppName=Lowtide
AppVersion={#AppVersion}
AppVerName=Lowtide {#AppVersion}
AppPublisher=phsrp
AppPublisherURL=https://github.com/phsrp/lowtide-fps
AppSupportURL=https://github.com/phsrp/lowtide-fps/issues
AppUpdatesURL=https://github.com/phsrp/lowtide-fps/releases
DefaultDirName={autopf}\Lowtide
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=admin
OutputDir=..\release
OutputBaseFilename=Lowtide-Setup-{#AppVersion}
SetupIconFile=..\assets\lowtide.ico
UninstallDisplayIcon={app}\Lowtide.exe
UninstallDisplayName=Lowtide
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=force
RestartApplications=no
VersionInfoVersion={#AppVersion}

[Tasks]
Name: startup; Description: "Start Lowtide with Windows"; Check: not IsUpgrade
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
Name: pawnio; Description: "Install the PawnIO driver (needed for CPU temperature)"; Check: not PawnIOInstalled

[Files]
Source: "..\dist\Lowtide\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "deps\PawnIO_setup.exe"; DestDir: "{tmp}"; Tasks: pawnio; Flags: deleteafterinstall

[InstallDelete]
; clear out files from the previous version's bundle
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Lowtide"; Filename: "{app}\Lowtide.exe"
Name: "{autodesktop}\Lowtide"; Filename: "{app}\Lowtide.exe"; Tasks: desktopicon

[Run]
; same as LibreHardwareMonitor: run the official PawnIO setup with -install
Filename: "{tmp}\PawnIO_setup.exe"; Parameters: "-install"; Tasks: pawnio; StatusMsg: "Installing the PawnIO driver..."; Flags: waituntilterminated skipifsilent
Filename: "{app}\Lowtide.exe"; Parameters: "--set-startup on"; Tasks: startup; Flags: runhidden waituntilterminated
Filename: "{app}\Lowtide.exe"; Description: "Start Lowtide now"; Flags: nowait postinstall skipifsilent
; silent installs are auto-updates: start the new version straight away
Filename: "{app}\Lowtide.exe"; Flags: nowait skipifnotsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /T /IM Lowtide.exe"; Flags: runhidden; RunOnceId: "StopLowtide"
Filename: "{app}\Lowtide.exe"; Parameters: "--set-startup off"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveStartup"

[Code]
var
  Upgrading: Boolean;

function IsUpgrade(): Boolean;
begin
  Result := Upgrading;
end;

function PawnIOInstalled(): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\PawnIO');
end;

function InitializeSetup(): Boolean;
begin
  Upgrading := RegKeyExists(HKLM, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\Lowtide_is1');
  Result := True;
end;
