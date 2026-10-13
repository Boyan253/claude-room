# Runs as SYSTEM (tailscaled always accepts SYSTEM, whoever holds the GUI lock).
# Publishes the room on the public internet through Tailscale Funnel.
param([string]$Action = "on")
$ts = "C:\Program Files\Tailscale\tailscale.exe"
$log = Join-Path $PSScriptRoot "funnel.log"
$port = 8765
try { $port = [int](Get-Content (Join-Path $PSScriptRoot "room.json") -Raw | ConvertFrom-Json).port } catch {}
"[$(Get-Date -Format s)] action=$Action" | Out-File $log -Encoding utf8
$self = (& $ts status --json | ConvertFrom-Json).Self
"node: $($self.DNSName)  online=$($self.Online)" | Out-File $log -Append -Encoding utf8
if ($Action -eq "diag") {
    $st = & $ts status --json | ConvertFrom-Json
    "caps: " + (($st.Self.CapMap.PSObject.Properties.Name | Where-Object { $_ -match "funnel|https|cert" }) -join ", ") | Out-File $log -Append -Encoding utf8
    "user: " + ($st.User.PSObject.Properties | ForEach-Object { $_.Value.LoginName } | Select-Object -Unique) -join ", " | Out-File $log -Append -Encoding utf8
    "certDomains: " + ($st.CertDomains -join ", ") | Out-File $log -Append -Encoding utf8
    & $ts debug prefs *>&1 | Select-String -Pattern "Ingress|WantRunning|ControlURL|ProfileName" | Out-File $log -Append -Encoding utf8
    & $ts serve status --json *>&1 | Out-File $log -Append -Encoding utf8
} elseif ($Action -eq "off") {
    & $ts funnel --https=443 off *>&1 | Out-File $log -Append -Encoding utf8
} else {
    # --bg persists across reboots; may print an enable-Funnel link and wait for it
    $job = Start-Job { & $using:ts funnel --bg $using:port *>&1 }
    $null = Wait-Job $job -Timeout 25
    Receive-Job $job *>&1 | Out-File $log -Append -Encoding utf8
    if ($job.State -eq "Running") { "STILL WAITING (needs enabling in admin console)" | Out-File $log -Append -Encoding utf8; Stop-Job $job }
}
"--- funnel status ---" | Out-File $log -Append -Encoding utf8
& $ts funnel status *>&1 | Out-File $log -Append -Encoding utf8
