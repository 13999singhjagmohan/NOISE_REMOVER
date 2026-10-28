; Inno Setup script: builds NoiseRemover-Setup-<version>.exe from dist\NoiseRemover
#define AppName "Noise Remover"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B0E4C1F-3A52-4D7C-9E61-2F4B8C9A7D10}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Noise Remover
DefaultDirName={autopf}\Noise Remover
DefaultGroupName=Noise Remover
UninstallDisplayIcon={app}\NoiseRemover.exe
SetupIconFile=app.ico
OutputDir=..\dist
OutputBaseFilename=NoiseRemover-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\NoiseRemover\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Noise Remover"; Filename: "{app}\NoiseRemover.exe"
Name: "{group}\Uninstall Noise Remover"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Noise Remover"; Filename: "{app}\NoiseRemover.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NoiseRemover.exe"; Description: "Launch Noise Remover"; Flags: nowait postinstall skipifsilent
