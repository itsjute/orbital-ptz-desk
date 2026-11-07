; Inno Setup script for the Orbital Windows installer.
; Built by .github/workflows/release.yml:  iscc /DAppVersion=1.2.3 /DStage=..\build\stage packaging\installer.iss
; <Stage> holds the components: main\ (program, go2rtc), ffmpeg\ (ffmpeg.exe), stabilize\ (numpy, OpenCV).

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef Stage
  #define Stage "..\build\stage"
#endif

[Setup]
AppId={{04C23277-668F-42F3-A390-37B7D802D5DC}
AppName=Orbital
AppVersion={#AppVersion}
AppVerName=Orbital {#AppVersion}
AppPublisher=Julian Teuber
AppPublisherURL=https://github.com/itsjute/ptz-desk
AppSupportURL=https://github.com/itsjute/ptz-desk/issues
AppUpdatesURL=https://github.com/itsjute/ptz-desk/releases
DefaultDirName={autopf}\Orbital
; versions before 1.1 were called "PTZ Desk" and lived in another folder
UsePreviousAppDir=no
DefaultGroupName=Orbital
DisableProgramGroupPage=yes
; installs for the current user without admin rights; the dialog offers "all users" too
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AppMutex=Orbital.SingleInstance,PTZDesk.SingleInstance
CloseApplications=yes
LicenseFile=..\LICENSE
SetupIconFile=orbital.ico
UninstallDisplayIcon={app}\Orbital.exe
UninstallDisplayName=Orbital
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
OutputDir=..\build\installer
OutputBaseFilename=Orbital-{#AppVersion}-Setup

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "de"; MessagesFile: "compiler:Languages\German.isl"

[CustomMessages]
en.TypeFull=Full installation
en.TypeCompact=Compact installation
en.TypeCustom=Custom installation
en.CompMain=Orbital (control page, video service go2rtc)
en.CompFfmpeg=FFmpeg – audio inputs of this computer (e.g. a USB audio interface), recording details; needed for stabilization
en.CompStabilize=Stabilization of recordings (NumPy, OpenCV)
de.TypeFull=Vollständige Installation
de.TypeCompact=Minimale Installation
de.TypeCustom=Benutzerdefinierte Installation
de.CompMain=Orbital (Steuerseite, Videodienst go2rtc)
de.CompFfmpeg=FFmpeg – Audioeingänge dieses Computers (z. B. USB-Audiointerface), Aufnahmedetails; nötig für die Stabilisierung
de.CompStabilize=Stabilisierung von Aufnahmen (NumPy, OpenCV)

[Types]
Name: "full"; Description: "{cm:TypeFull}"
Name: "compact"; Description: "{cm:TypeCompact}"
Name: "custom"; Description: "{cm:TypeCustom}"; Flags: iscustom

[Components]
Name: "main"; Description: "{cm:CompMain}"; Types: full compact custom; Flags: fixed
Name: "ffmpeg"; Description: "{cm:CompFfmpeg}"; Types: full
Name: "stabilize"; Description: "{cm:CompStabilize}"; Types: full

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; start every update from a clean program folder (settings live in %APPDATA%\Orbital)
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\ffmpeg.exe"; Components: not ffmpeg
; the program as it was called before 1.1 ("PTZ Desk"); its settings move to %APPDATA%\Orbital on first start
Type: filesandordirs; Name: "{autopf}\PTZ Desk"
Type: files; Name: "{autoprograms}\PTZ Desk.lnk"
Type: files; Name: "{autodesktop}\PTZ Desk.lnk"

[Files]
Source: "{#Stage}\main\*"; DestDir: "{app}"; Components: main; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Stage}\ffmpeg\*"; DestDir: "{app}"; Components: ffmpeg; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#Stage}\stabilize\*"; DestDir: "{app}"; Components: stabilize; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Orbital"; Filename: "{app}\Orbital.exe"
Name: "{autodesktop}\Orbital"; Filename: "{app}\Orbital.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Orbital.exe"; Description: "{cm:LaunchProgram,Orbital}"; Flags: nowait postinstall skipifsilent
