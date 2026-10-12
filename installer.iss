; ClipBoardSync installer. Build the app first:
;   python build_exe.py --mode onedir --target gui
; then compile this script with Inno Setup 6.
#define MyAppName "ClipBoardSync"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "ClipBoardSync"
#define MyAppExeName "ClipBoardSync.exe"

[Setup]
AppId={{B4C2A6E1-7F3D-4A5B-9C8D-ClipBoardSync01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=ClipBoardSync-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "autostart"; Description: "Start sharing when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\ClipBoardSync\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start sharing now"; Flags: nowait postinstall skipifsilent
