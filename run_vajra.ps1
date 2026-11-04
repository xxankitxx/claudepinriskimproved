# =============================================================================
# VAJRA 2.0 operator launcher (Windows / PowerShell 5.1+). ASCII-only on purpose:
# Windows PowerShell 5.1 reads BOM-less files as ANSI and can mis-parse non-ASCII text.
#
# Usage:
#   .\run_vajra.ps1 -Token 12345678          # live session with today's Breeze SESSION_KEY
#   .\run_vajra.ps1                          # live (token from env VAJRA_SESSION_KEY / vajra\.token / credentials.py)
#   .\run_vajra.ps1 -SkipVerify              # mid-day restart: skip the ~20 s completion gate
#   .\run_vajra.ps1 -Mode synthetic          # dev smoke (NON-BINDING data)
#   .\run_vajra.ps1 -Mode replay -Date 2026-10-15   # replay a recorded day into tapes_replay\
#   .\run_vajra.ps1 -Verify                  # run the completion gate only
#   .\run_vajra.ps1 -NoBrowser               # do not open the sound-enabled dashboard window
#
# 2.0.2: the launcher opens the dashboard in its own Chrome/Edge app window started with
# --autoplay-policy=no-user-gesture-required, so trade sounds work WITHOUT a click (sound council).
# =============================================================================
param(
    [ValidateSet("live","replay","synthetic")] [string]$Mode = "live",
    [string]$Date = "",
    [string]$Token = "",
    [switch]$NoDashboard,
    [switch]$SkipVerify,
    [switch]$Verify,
    [switch]$NoBrowser
)

Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = "utf-8"

if ($Verify) {
    python verify_vajra.py
    exit $LASTEXITCODE
}

Write-Host "VAJRA 2.0 - pin-risk / gamma-explosion engine" -ForegroundColor Yellow
Write-Host "mode=$Mode  dashboard=$(-not $NoDashboard)  (LIVE_ORDERS governed by vajra_config.yaml)"

if (-not $SkipVerify) {
    python verify_vajra.py
    if ($LASTEXITCODE -ne 0) { Write-Host "Completion gate FAILED - refusing to run." -ForegroundColor Red; exit 1 }
}

if ($Mode -eq "live") {
    if ($Date -ne "") { Write-Host "-Date is not allowed in live mode." -ForegroundColor Red; exit 1 }
    if ($Token -eq "" -and -not $env:VAJRA_SESSION_KEY -and -not (Test-Path "vajra/.token") -and -not (Test-Path "vajra/credentials.py")) {
        Write-Host "No Breeze session token: pass -Token <apisession> (or set VAJRA_SESSION_KEY / write vajra\.token)." -ForegroundColor Red
        exit 1
    }
    if ($Token -eq "") {
        Write-Host "Reminder: the Breeze SESSION_KEY expires daily - pass today's with -Token <apisession>." -ForegroundColor Yellow
    }
}

function Start-VajraDashboardWindow {
    # A background job waits for the dashboard to answer, then opens ONE app window (own profile,
    # sound allowed without a gesture, no background throttling). Never opens twice per launcher run.
    $port = 8766
    try {
        $m = Select-String -Path "vajra_config.yaml" -Pattern '^\s*port:\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    } catch {}
    $url = "http://127.0.0.1:$port/"
    $exe = $null
    foreach ($c in @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                     "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                     "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
                     "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                     "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe")) {
        if ($c -and (Test-Path $c)) { $exe = $c; break }
    }
    if (-not $exe) {
        Write-Host "Chrome/Edge not found - open $url yourself and CLICK the page once to enable sound." -ForegroundColor Yellow
        return
    }
    $profileDir = Join-Path $PSScriptRoot ".dashboard_profile"
    $chromeArgs = @("--app=$url", "--user-data-dir=$profileDir", "--autoplay-policy=no-user-gesture-required",
                    "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                    "--disable-backgrounding-occluded-windows", "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
                    "--no-first-run", "--no-default-browser-check")
    Start-Job -ScriptBlock {
        param($exe, $chromeArgs, $url)
        for ($i = 0; $i -lt 180; $i++) {
            try { $r = Invoke-WebRequest -Uri ($url + "api/health") -UseBasicParsing -TimeoutSec 2
                  if ($r.StatusCode -eq 200) { Start-Process -FilePath $exe -ArgumentList $chromeArgs; return } } catch {}
            Start-Sleep -Seconds 2
        }
    } -ArgumentList $exe, $chromeArgs, $url | Out-Null
    Write-Host "Dashboard window will open at $url (sound enabled, no click needed)." -ForegroundColor Green
}

$Mode = $Mode.ToLower()     # ValidateSet is case-insensitive, argparse is not
if (-not $NoDashboard -and -not $NoBrowser) { Start-VajraDashboardWindow }
$pyArgs = @("-m","vajra.app","--mode",$Mode)
if ($Date -ne "")     { $pyArgs += @("--date",$Date) }
if ($Token -ne "")    { $pyArgs += @("--token",$Token) }
if ($NoDashboard)     { $pyArgs += "--no-dashboard" }
if ($Mode -ne "live") {
    python @pyArgs
    exit $LASTEXITCODE
}
# LIVE: if the engine dies during market hours (broker websocket refused, crash), relaunch it.
# A relaunch restores capital / open paper position / cooldowns from today's order tape.
# Stops relaunching from 15:38 IST (the closing-auction square-off; F&O close 15:40), or after 3 quick failures in a row (bad token, holiday...).
$quick = 0
while ($true) {
    $t0 = Get-Date
    python @pyArgs
    $code = $LASTEXITCODE
    if ($code -eq 0) { exit 0 }
    $now = Get-Date     # the operator's PC runs on IST
    if ($now.Hour -gt 15 -or ($now.Hour -eq 15 -and $now.Minute -ge 38)) { exit $code }
    if (((Get-Date) - $t0).TotalSeconds -lt 60) { $quick++ } else { $quick = 0 }
    if ($quick -ge 3) {
        Write-Host "VAJRA failed 3 times in a row within a minute - not relaunching. Check the token / errors above." -ForegroundColor Red
        exit $code
    }
    Write-Host "VAJRA exited with code $code during market hours - relaunching in 20 s (Ctrl+C to cancel)..." -ForegroundColor Yellow
    Start-Sleep -Seconds 20
}
