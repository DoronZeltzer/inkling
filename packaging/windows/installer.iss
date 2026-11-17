; Inno Setup recipe: wraps dist\Inkling into one installer (no administrator rights needed).
#ifndef Version
  #define Version "0.0.0"
#endif
[Setup]
AppName=Inkling
AppVersion={#Version}
AppPublisher=Inkling
DefaultDirName={autopf}\Inkling
DefaultGroupName=Inkling
OutputDir=..\..\dist
OutputBaseFilename=Inkling-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=inkling.ico
UninstallDisplayIcon={app}\Inkling.exe

[Files]
Source: "..\..\dist\Inkling\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Put an Inkling icon on the desktop"

[Icons]
Name: "{group}\Inkling"; Filename: "{app}\Inkling.exe"
Name: "{autodesktop}\Inkling"; Filename: "{app}\Inkling.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Inkling.exe"; Description: "Open Inkling"; Flags: nowait postinstall skipifsilent
