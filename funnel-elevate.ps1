# Elevated helper: runs funnel.ps1 as SYSTEM through a one-shot scheduled task.
param([string]$Action = "on")
$script = Join-Path $PSScriptRoot "funnel.ps1"
$out = Join-Path $PSScriptRoot "funnel-elevate.log"
$tr = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Action $Action"
schtasks /Create /F /RU SYSTEM /SC ONCE /ST 00:00 /TN ClaudeRoomFunnel /TR $tr *>&1 | Out-File $out -Encoding utf8
schtasks /Run /TN ClaudeRoomFunnel *>&1 | Out-File $out -Append -Encoding utf8
Start-Sleep 40
schtasks /Delete /F /TN ClaudeRoomFunnel *>&1 | Out-File $out -Append -Encoding utf8
