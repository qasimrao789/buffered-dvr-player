#define MyAppName "StreamShift DVR"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "qasimrao789"
#define MyAppExeName "StreamShift DVR.exe"

[Setup]
AppId={{EFA09C91-CC6E-4EA7-8EE7-55BC0E52D6F1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\StreamShift DVR
DefaultGroupName=StreamShift DVR
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=release
OutputBaseFilename=StreamShift-DVR-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
VersionInfoVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "dist\StreamShift DVR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\StreamShift DVR"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\StreamShift DVR"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch StreamShift DVR"; Flags: nowait postinstall
