# Creates a silent launcher: desktop shortcut -> pythonw (NO console window).
# Re-run this any time (e.g. after a Python upgrade changes the pythonw path).
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\make_shortcut.ps1

$ErrorActionPreference = "Stop"

# Project root = parent of this script's directory
$root = Split-Path -Parent $PSScriptRoot

# Resolve pythonw.exe next to the current interpreter (no console flash)
$pythonDir = Split-Path -Parent (Get-Command python).Source
$pythonw = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = (Get-Command python).Source }

$icon = Join-Path $root "assets\app.ico"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "AgentAssistant.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m agent_assistant.main"
$lnk.WorkingDirectory = $root          # .env is read relative to CWD
if (Test-Path $icon) { $lnk.IconLocation = "$icon,0" }
$lnk.Description = "AgentAssistant - study/research desktop agent"
$lnk.Save()

Write-Host "created: $lnkPath"
Write-Host "target : $pythonw -m agent_assistant.main"
Write-Host "workdir: $root"
