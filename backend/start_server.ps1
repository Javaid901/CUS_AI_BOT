# backend/start_server.ps1
# Single entry point for the CUS AI backend on port 8001.
#
# Fixes the "[Errno 10048] address already in use" error:
#   1. Finds the instance to stop by LISTENING PORT (catches uvicorn started
#      manually, via pythonw, or by any other launcher) in addition to the
#      process command line.
#   2. Waits until the port is actually free before starting (up to 30s) so a
#      dying instance's socket can never collide with the new one.
#   3. Starts exactly one instance and verifies it came up.
#
# PostgreSQL (embedded, port/DATABASE_URL from backend/.env):
#   Before starting the API the script ensures the existing embedded PostgreSQL
#   cluster is up, waits for it to accept connections, and verifies the
#   configured database with SELECT 1 and read-only table counts.
#   The postmaster is launched detached via WMI (Win32_Process.Create), which
#   escapes restricted/container process contexts (job objects) that otherwise
#   make PostgreSQL backends fail to spawn (0xC0000142 / error 487).
#   uvicorn is launched the same way (WMI -> transient helper -> Start-Process
#   with log redirection), so the whole backend survives the launching
#   terminal closing and does not depend on any short-lived shell.
#   Idempotent: already running -> verify only; stopped -> start; starting ->
#   wait; cannot start -> abort with a clear message BEFORE uvicorn runs.
#   Skip with -SkipPostgres. Override locations with $env:CUS_AI_PG_BIN and
#   $env:CUS_AI_PGDATA.
#
# Usage (from anywhere):
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1  -Port 9000
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1  -Workers 2     # multi-worker (Phase 3C-5)
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1  -KillOnly        # just stop, don't start
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1  -SkipPostgres    # don't touch PostgreSQL
#   powershell -ExecutionPolicy Bypass -File .\start_server.ps1  -GracefulSeconds 5   # bounded graceful shutdown
#
# Graceful shutdown: uvicorn's default timeout_graceful_shutdown=None makes a
# graceful stop (SIGINT/CTRL+C, SIGHUP, worker processes) wait FOREVER while
# this app keeps permanent SSE streams open (/api/admin/jobs/events global +
# per-job, chat heartbeat, ingest SSE). An open stream keeps uvicorn's
# server_state.connections/tasks non-empty, so the process never exits and
# keeps TCP port 8001 bound -> the next start collides and someone must kill
# the PID manually. Passing --timeout-graceful-shutdown bounds that wait: the
# hung SSE tasks are cancelled, lifespan shutdown runs, and the process exits
# cleanly, freeing the port. (See backend/tests/test_server_clean_exit.py.)

param(
    [int]$Port = 8001,
    [int]$Workers = 0,
    [switch]$KillOnly,
    [switch]$SkipPostgres,
    [int]$GracefulSeconds = 10
)

$ErrorActionPreference = "SilentlyContinue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

# ---------------------------------------------------------------------------
# PostgreSQL ensure (embedded cluster)
# ---------------------------------------------------------------------------
if ($KillOnly) {
    $SkipPostgres = $true
}
if (-not $SkipPostgres) {
    $envFile = Join-Path $ScriptDir ".env"
    $dbUrl = $env:DATABASE_URL
    if (-not $dbUrl) {
        $line = Get-Content $envFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*DATABASE_URL\s*=' } |
            Select-Object -First 1
        if ($line) { $dbUrl = ($line -replace '^\s*DATABASE_URL\s*=\s*', '').Trim().Trim('"') }
    }

    $m = $null
    if ($dbUrl) { $m = [regex]::Match($dbUrl, '^postgresql(?:\+\w+)?://(?:([^@/]*)@)?([^:/]+):(\d+)/(\w+)(?:\?.*)?$') }

    if (-not $dbUrl -or -not $m.Success) {
        if ($dbUrl -and $dbUrl -match '^sqlite') {
            Write-Host "== PostgreSQL: DATABASE_URL is SQLite - skipping PostgreSQL ensure =="
        } else {
            Write-Host "== PostgreSQL: no PostgreSQL DATABASE_URL found - skipping PostgreSQL ensure =="
        }
    } else {
        $pgUser = $m.Groups[1].Value
        if ($pgUser -and $pgUser -match ':') { $pgUser = ($pgUser -split ':')[0] }
        if (-not $pgUser) { $pgUser = "postgres" }
        $pgHost = $m.Groups[2].Value
        $pgPort = [int]$m.Groups[3].Value
        $pgDb   = $m.Groups[4].Value

        # --- resolve binaries ------------------------------------------------
        $pgBin = $env:CUS_AI_PG_BIN
        if (-not $pgBin) {
            $probe = & python -c "import os, embedded_postgres; print(os.path.join(os.path.dirname(embedded_postgres.__file__), 'pginstall', 'bin'))" 2>$null
            if ($probe -and (Test-Path (Join-Path $probe "pg_ctl.exe"))) { $pgBin = $probe.Trim() }
        }
        if (-not $pgBin) {
            $cand = Join-Path $env:APPDATA "Python\Python314\site-packages\embedded_postgres\pginstall\bin"
            if (Test-Path (Join-Path $cand "pg_ctl.exe")) { $pgBin = $cand }
        }
        if (-not $pgBin -or -not (Test-Path (Join-Path $pgBin "pg_ctl.exe"))) {
            Write-Host "  FATAL: PostgreSQL binaries not found - set CUS_AI_PG_BIN or install embedded-postgres."
            exit 1
        }

        # --- resolve data directory ------------------------------------------
        $pgData = $env:CUS_AI_PGDATA
        if (-not $pgData) { $pgData = Join-Path $env:TEMP "opencode\p3c2\pgdata" }
        if (-not (Test-Path (Join-Path $pgData "PG_VERSION"))) {
            Write-Host "  FATAL: PostgreSQL data directory not found or invalid: $pgData"
            exit 1
        }

        $pgCtl    = Join-Path $pgBin "pg_ctl.exe"
        $pgIsReady = Join-Path $pgBin "pg_isready.exe"
        $psql     = Join-Path $pgBin "psql.exe"
        $logFile  = Join-Path $pgData "log"

        Write-Host "== PostgreSQL ensure (host=$pgHost port=$pgPort db=$pgDb data=$pgData) =="

        $readyProbe = {
            & $pgIsReady -h $pgHost -p $pgPort *> $null
            $LASTEXITCODE -eq 0
        }

        if (& $readyProbe) {
            Write-Host "  PostgreSQL already running on $pgHost`:$pgPort - verifying database"
        } else {
            & $pgCtl -D $pgData status *> $null
            $pgStatus = $LASTEXITCODE
            if ($pgStatus -eq 0) {
                $pidLines = Get-Content (Join-Path $pgData "postmaster.pid") -ErrorAction SilentlyContinue
                $dataPort = $null
                if ($pidLines -and $pidLines.Count -ge 4) { $dataPort = [int]$pidLines[3].Trim() }
                if ($dataPort -and $dataPort -ne $pgPort) {
                    Write-Host "  FATAL: PostgreSQL is running but listening on port $dataPort, not the configured port $pgPort."
                    exit 1
                }
                Write-Host "  PostgreSQL process is up - waiting for it to accept connections on port $pgPort"
            } else {
                Write-Host "  PostgreSQL not running - starting the existing cluster detached via WMI"
                $startCmd = '"{0}" -D "{1}" -l "{2}" -o "-p {3} -h {4}" -w start' -f $pgCtl, $pgData, $logFile, $pgPort, $pgHost
                $wmi = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $startCmd }
                if (-not $wmi -or $wmi.ReturnValue -ne 0) {
                    Write-Host ("  FATAL: could not launch PostgreSQL via WMI (ReturnValue=" + $wmi.ReturnValue + ").")
                    exit 1
                }
                Write-Host ("  started launcher PID " + $wmi.ProcessId + " - waiting for readiness")
            }

            $ready = $false
            for ($i = 0; $i -lt 90; $i++) {
                Start-Sleep -Seconds 1
                if (& $readyProbe) { $ready = $true; break }
                if (($i % 15) -eq 14) { Write-Host ("    ... still waiting (" + ($i + 1) + "s)") }
            }
            if (-not $ready) {
                Write-Host "  FATAL: PostgreSQL did not become ready on port $pgPort within 90s. Tail of log:"
                if (Test-Path $logFile) { Get-Content $logFile -Tail 10 }
                exit 1
            }
            Write-Host "  PostgreSQL is ready"
        }

        # --- verify configured database is reachable and not empty ------------
        $one = (& $psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -t -A -c "SELECT 1" 2>$null | Select-Object -First 1).Trim()
        if ($one -ne "1") {
            Write-Host "  FATAL: database '$pgDb' is not reachable on $pgHost`:$pgPort (SELECT 1 failed)."
            if (Test-Path $logFile) { Get-Content $logFile -Tail 8 }
            exit 1
        }
        $countsSql = "SELECT (SELECT count(*) FROM university_documents) AS university_documents, " +
                     "(SELECT count(*) FROM website_pages) AS website_pages, " +
                     "(SELECT count(*) FROM documents) AS documents, " +
                     "(SELECT count(*) FROM date_sheet_entries) AS date_sheet_entries"
        $counts = (& $psql -h $pgHost -p $pgPort -U $pgUser -d $pgDb -t -A -c $countsSql 2>$null | Select-Object -First 1).Trim()
        if ($counts) {
            Write-Host ("  DB check: SELECT 1 OK; counts (university_documents, website_pages, documents, date_sheet_entries) = " + $counts)
        } else {
            Write-Host "  FATAL: SELECT 1 OK but reading expected data tables failed - cluster does not look like cus_ai."
            exit 1
        }
    }
}

Write-Host "== Stopping all instances on port $Port =="
$found = @(
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "uvicorn" }
) + @(
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue } |
        Where-Object { $_.ProcessName -match "python" } |
        ForEach-Object {
            Get-CimInstance Win32_Process -Filter "ProcessId = $($_.Id)"
        }
)
$seen = @{}
foreach ($p in $found) {
    if ($p -and -not $seen.ContainsKey($p.ProcessId)) {
        $seen[$p.ProcessId] = $true
        Write-Host ("  stopping PID " + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force
    }
}
if ($seen.Count -eq 0) { Write-Host "  nothing running on port $Port" }

Write-Host "== Waiting for port $Port to free up =="
$free = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if (-not $listener) { $free = $true; break }
}
if ($free) {
    Write-Host "  port $Port is free"
} else {
    Write-Host "  WARNING: port $Port still busy after 15s - a stray process may hold it."
    if ($KillOnly) { exit 1 }
}

if ($KillOnly) {
    Write-Host "== Done (kill-only mode) =="
    exit 0
}

Write-Host "== Starting uvicorn on port $Port (detached via WMI) =="
$outLog = Join-Path $ScriptDir "server_out.log"
$errLog = Join-Path $ScriptDir "server_err.log"
$argItems = @("'-m'", "'uvicorn'", "'app.main:app'", "'--host'", "'0.0.0.0'", "'--port'", "'$Port'")
if ($GracefulSeconds -gt 0) {
    $argItems += "'--timeout-graceful-shutdown'"; $argItems += "'$GracefulSeconds'"
}
if ($Workers -gt 1) {
    $argItems += "'--workers'"; $argItems += "'$Workers'"
}
$inner = "Start-Process -FilePath 'python' -ArgumentList @(" + ($argItems -join ",") + ") -WorkingDirectory '" + $ScriptDir + "' -RedirectStandardOutput '" + $outLog + "' -RedirectStandardError '" + $errLog + "' -WindowStyle Hidden"
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($inner))
$wmiCmd = 'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + $enc
$wmi = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $wmiCmd }
if (-not $wmi -or $wmi.ReturnValue -ne 0) {
    Write-Host ("  FATAL: could not launch uvicorn via WMI (ReturnValue=" + $wmi.ReturnValue + ").")
    exit 1
}
Write-Host ("  uvicorn launcher PID " + $wmi.ProcessId + " - waiting for port $Port (cold import can take a while)")

$up = $null
for ($i = 0; $i -lt 120; $i++) {
    if ($i -gt 0) { Start-Sleep -Seconds 1 }
    $up = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($up) { break }
    if (($i % 15) -eq 14) { Write-Host ("    ... still waiting (" + ($i + 1) + "s)") }
}
if ($up) {
    Write-Host ("== Server is UP on http://127.0.0.1:$Port (PID " + $up.OwningProcess + ") ==")
} else {
    Write-Host "== Server did not come up within 120s. Last lines of server_err.log: =="
    Get-Content $errLog -Tail 20
    Write-Host "== Last lines of server_out.log: =="
    Get-Content $outLog -Tail 20
    exit 1
}