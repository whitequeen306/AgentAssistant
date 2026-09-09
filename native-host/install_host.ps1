# Registers the AgentAssistant native messaging host for Microsoft Edge (J1, 08 §4.5).
#
# Usage:
#   1. Load the unpacked extension: edge://extensions → Developer mode →
#      "Load unpacked" → select the browser-extension/ folder.
#   2. Copy the extension ID shown on the card (a 32-char string).
#   3. Run:  ./install_host.ps1 -ExtensionId <that-id>
#   4. Reload the extension (or restart Edge) so it picks up the host.
#
# What this does:
#   - Generates agent_assistant_host.bat (native messaging `path` must be a
#     single executable; the .bat wraps `python <host_script>`).
#   - Generates com.agent_assistant.host.json (the host manifest) with
#     allowed_origins locked to your extension ID.
#   - Writes HKCU:\...\Edge\NativeMessagingHosts\com.agent_assistant.host
#     (default) = absolute path to the manifest JSON.

param(
    [Parameter(Mandatory = $true)]
    [string]$ExtensionId
)

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$hostScript = Join-Path $here "agent_assistant_host.py"
$batPath = Join-Path $here "agent_assistant_host.bat"
$manifestPath = Join-Path $here "com.agent_assistant.host.json"

# Resolve the python interpreter (prefer `python`, fall back to `py`)
$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $python = (Get-Command py -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
    throw "Python interpreter not found on PATH. Install Python or add it to PATH."
}

# 1. .bat launcher — native messaging `path` must be one executable.
#    Encoding Default (ANSI) so non-ASCII install paths survive cmd.exe.
$bat = "@echo off`r`n`"$python`" `"$hostScript`""
Set-Content -Path $batPath -Value $bat -Encoding Default

# 2. Host manifest JSON (allowed_origins locked to this extension ID).
#    BOM-less UTF-8 — technically valid JSON for Edge.
$manifest = @{
    name           = "com.agent_assistant.host"
    description    = "AgentAssistant native messaging host"
    path           = $batPath
    type           = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json -Compress
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($manifestPath, $manifest, $utf8NoBom)

# 3. Register in the Edge native messaging registry (per-user, no admin needed)
$key = "HKCU:\Software\Microsoft\Edge\NativeMessagingHosts\com.agent_assistant.host"
New-Item -Path $key -Force | Out-Null
Set-ItemProperty -Path $key -Name "(default)" -Value $manifestPath

Write-Host "Installed AgentAssistant native messaging host for Edge." -ForegroundColor Green
Write-Host "  python:    $python"
Write-Host "  bat:       $batPath"
Write-Host "  manifest:  $manifestPath"
Write-Host "  registry:  $key"
Write-Host "  extension: $ExtensionId"
Write-Host ""
Write-Host "Reload the extension (or restart Edge) for it to take effect." -ForegroundColor Yellow
