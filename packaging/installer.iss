; Inno Setup script for the CloudTransfer Windows installer.
;
; Build order:
;   pyinstaller packaging\cloudtransfer.spec --noconfirm
;   iscc packaging\installer.iss
;
; Produces dist\installer\CloudTransfer-Setup-<version>.exe, a per-user
; install that needs no administrator rights: everything lands under the
; user's own profile, which is also where the application keeps its database.

#define AppName       "CloudTransfer"
; Overridable from the command line so a release build can carry the tag:
;   iscc /DAppVersion=1.2.3 packaging\installer.iss
#ifndef AppVersion
  #define AppVersion  "1.4.0"
#endif

; VersionInfoVersion accepts only one to four dot-separated numbers, so a
; pre-release version such as "1.0.0-rc1" has to have its suffix stripped for
; that one field. AppVersion keeps the full string, since that is what people
; see and what names the installer file.
#define DashPos Pos("-", AppVersion)
#if DashPos == 1
  #define NumericVersion "0.0.0"
#elif DashPos > 1
  #define NumericVersion Copy(AppVersion, 1, DashPos - 1)
#else
  #define NumericVersion AppVersion
#endif

#define AppPublisher  "CloudTransfer"
#define AppExeName    "CloudTransfer.exe"
#define AppId         "{{7C4F2E18-9B3A-4D62-9E4D-2F8A1C5B6D70}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#NumericVersion}
VersionInfoTextVersion={#AppVersion}

; CloudTransfer runs from the notification area, so its files stay locked while
; it is open and an install or uninstall cannot replace them. The application
; holds this named mutex for as long as it runs (APP_MUTEX_NAME in
; cloudtransfer/desktop.py - the two names must match), which lets Setup say so
; plainly instead of failing partway through on a file it could not write.
AppMutex=CloudTransferSingleInstance
; Belt and braces: also ask Restart Manager to close anything still holding a
; file, for an instance started in a way the mutex check missed.
CloseApplications=yes
CloseApplicationsFilter=*.exe,*.dll,*.pyd
RestartApplications=no
; Two installers running at once would fight over the same files.
SetupMutex=CloudTransferSetup

; Per-user install: no UAC prompt, no administrator account needed.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto

OutputDir=..\dist\installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=cloudtransfer.ico
UninstallDisplayIcon={app}\{#AppExeName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; "x64compatible" replaced the older "x64" in Inno Setup 6.3; accept both so
; the build does not depend on which version the runner happens to install.
#if VER >= EncodeVer(6,3,0)
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
#else
ArchitecturesInstallIn64BitMode=x64
ArchitecturesAllowed=x64
#endif

; The application talks to Google and Microsoft over the network; say so
; before anyone is asked to trust an unsigned installer.
AppComments=Transfers files from Google Drive to OneDrive without saving them to disk.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "turkish";  MessagesFile: "compiler:Languages\Turkish.isl"

[Messages]
; The stock wording just says "close all instances", which is no help when the
; application has no window - it lives in the notification area. Say where it
; actually is, and what to do if it cannot be found there.
english.SetupAppRunningError=CloudTransfer is still running, so Setup cannot replace its files.%n%nRight-click the CloudTransfer icon in the notification area (bottom-right of the taskbar - you may need to click the small arrow to show hidden icons) and choose Quit.%n%nIf you cannot find it there, open Task Manager, find CloudTransfer.exe on the Details tab and end it.%n%nThen click OK to continue, or Cancel to exit.
english.UninstallAppRunningError=CloudTransfer is still running, so it cannot be removed.%n%nRight-click the CloudTransfer icon in the notification area (bottom-right of the taskbar - you may need to click the small arrow to show hidden icons) and choose Quit.%n%nIf you cannot find it there, open Task Manager, find CloudTransfer.exe on the Details tab and end it.%n%nThen click OK to continue, or Cancel to exit.
turkish.SetupAppRunningError=CloudTransfer hala calisiyor, bu yuzden dosyalari degistirilemiyor.%n%nGorev cubugunun sag alt kosesindeki bildirim alaninda CloudTransfer simgesine sag tiklayip Quit deyin (simge gizliyse kucuk ok isaretine tiklayin).%n%nSimgeyi bulamazsaniz Gorev Yoneticisi'ni acip Ayrintilar sekmesinde CloudTransfer.exe surecini sonlandirin.%n%nArdindan devam etmek icin Tamam'a basin.
turkish.UninstallAppRunningError=CloudTransfer hala calisiyor, bu yuzden kaldirilamiyor.%n%nGorev cubugunun sag alt kosesindeki bildirim alaninda CloudTransfer simgesine sag tiklayip Quit deyin (simge gizliyse kucuk ok isaretine tiklayin).%n%nSimgeyi bulamazsaniz Gorev Yoneticisi'ni acip Ayrintilar sekmesinde CloudTransfer.exe surecini sonlandirin.%n%nArdindan devam etmek icin Tamam'a basin.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
    GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start CloudTransfer when I sign in"; \
    GroupDescription: "Startup"; Flags: unchecked

[Files]
; The whole PyInstaller one-folder build.
Source: "..\dist\CloudTransfer\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; The GPL requires the licence to accompany the program, so it is installed
; alongside it rather than only shown during setup.
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Only the application's own logs. The database holds connected accounts and
; transfer history, so it is left alone unless the user asks for it below.
Type: files; Name: "{localappdata}\{#AppName}\cloudtransfer.log*"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\{#AppName}');
    if DirExists(DataDir) then
    begin
      if MsgBox('Also remove saved settings, connected accounts and transfer history?' + #13#10 +
                'Files already copied to OneDrive are not affected.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
