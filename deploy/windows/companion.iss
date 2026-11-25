; Companion AI OS — Inno Setup 6 Windows Desktop Installer Script
; Builds a zero-admin per-user Windows .exe installer (`CompanionSetup.exe`)
; Application code installs to: %LOCALAPPDATA%\Programs\Companion
; User data stays isolated in : %LOCALAPPDATA%\Companion\data (preserved across upgrades & uninstalls)

#define MyAppName "Companion AI OS"
#define MyAppVersion "5.0.1"
#define MyAppPublisher "Companion AI OS Contributors"
#define MyAppExeName "launch-companion.vbs"

[Setup]
AppId={{8E4C7D29-5F3B-4A91-B8C2-9A61D3E57F10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Companion
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
LicenseFile=..\..\LICENSE
OutputDir=..\..\dist
OutputBaseFilename=CompanionSetup-v{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "Start Companion AI OS automatically in the Windows System Tray when I sign in"; GroupDescription: "Startup Behavior:"; Flags: unchecked

[Dirs]
Name: "{localappdata}\Companion\data"

[Files]
Source: "..\..\dist\stage\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; WorkingDir: "{app}"; Comment: "Launch Companion AI OS in System Tray & Browser"
Name: "{group}\Open User Data Folder"; Filename: "{localappdata}\Companion\data"; Comment: "Open Companion isolated user data folder"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{sys}\wscript.exe"; Parameters: """{app}\launch-companion-silent.vbs"""; WorkingDir: "{app}"; Tasks: startup

[Run]
Filename: "{sys}\wscript.exe"; Parameters: """{app}\{#MyAppExeName}"""; WorkingDir: "{app}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent
