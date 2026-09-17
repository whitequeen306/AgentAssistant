; Inno Setup 脚本 — 词元新学 AgentAssistant 安装程序
; 用法：安装 Inno Setup 6（免费开源 https://jrsoftware.org）后，
;       右键本文件 → Compile，或直接命令行：
;       ISCC.exe packaging\installer.iss
; 输出：dist-installer\AgentAssistant-Setup.exe

#define MyAppName "词元新学"
#define MyAppPublisher "AgentAssistant"
#define MyAppVersion "0.1.0"
#define MyAppExeName "AgentAssistant.exe"

[Setup]
AppId={{6F2B4C91-3D5A-4E88-9C1B-7A0E5D2F4B31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\AgentAssistant
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=auto
DisableDirPage=auto
OutputDir=dist-installer
OutputBaseFilename=AgentAssistant-Setup
SetupIconFile=assets\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
WizardResizable=no
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked
Name: "quicklaunchicon"; Description: "创建任务栏快捷方式"; GroupDescription: "附加图标:"; Flags: unchecked; OnlyBelowVersion: 6.1

[Files]
Source: "dist-exe\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Messages]
chinesesimp.SetupAppTitle=安装 {#MyAppName}
chinesesimp.SetupAppMutex=安装程序已在运行
chinesesimp.WelcomeLabel1=欢迎安装 {#MyAppName}
chinesesimp.WelcomeLabel2=大学生自主学习智能体平台，数据全部保存在本机。
chinesesimp.FinishedHeadingLabel=安装完成
