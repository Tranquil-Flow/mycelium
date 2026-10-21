# SPDX-License-Identifier: AGPL-3.0-only
<#
.SYNOPSIS
A13 Windows device bring-up: arrival-script Phase 1 plus a hardware survey.

.DESCRIPTION
Runs, on the Windows device itself, the Phase 1 portable-source gates of the A13
device-readiness arrival script and writes ONE JSON result file:

  1.1 OS / architecture floor (Windows 10.0 build >= 19045, AMD64), including
      the tranche's own admit_windows_platform decision on the observed values
  1.2 toolchain: CPython 3.14.x, git, pytest + cryptography importable
  1.3 materialise the exact commit (LF line endings, core.autocrlf=false) and
      run the A6 source-manifest --check
  1.4 the seven Windows source-tranche acceptance files, then the later
      Windows-lane acceptance files
  1.5 the whole A13 acceptance package
  1.6 POSIX-primitive gap probes (O_NOFOLLOW, directory fsync, getuid, killpg,
      the real icacls/whoami ACL reader, the real FlushFileBuffers flush, the
      real atomic join write) with raw whoami/icacls text captured as fixtures
  hw  hardware survey via Get-CimInstance (CPU, cores, RAM, GPU/VRAM, disks,
      OS build, architecture)

Claim boundary: device bring-up and portable-source gates only. Nothing here
builds, signs, installs, registers or starts a service, joins a swarm, or runs a
physical-matrix case. The pilot installer is Authenticode-UNSIGNED by owner
ruling; this script never claims otherwise. It never prints or reads secrets.

Exit code: 0 only when every gate passed; 1 otherwise (including when the
script itself stopped early -- the JSON result still records how far it got).
RepoPath should be a dedicated checkout: the script detaches it at the exact
commit and refuses (never discards) local changes.

.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\a13_windows_device_bringup.ps1 `
  -RepoPath C:\mycelium\core-pilot -SourceBundle D:\core-pilot.bundle `
  -ExpectedCommit <40-hex commit> -OutputPath C:\mycelium\bringup-result.json
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RepoPath,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$ExpectedCommit,
    [Parameter(Mandatory = $true)][string]$OutputPath,
    [string]$SourceBundle = '',
    [ValidatePattern('^[0-9a-f]{40}$')][string]$A6Base = '3282983100595dfc8a730a06cf0cf5c750219e32',
    [string]$PythonExe = '',
    [switch]$InstallPythonDeps,
    [int]$StepTimeoutSeconds = 1800
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Protocol = 'mycelium.a13_windows_device_bringup.v1'
$ClaimBoundary = 'device bring-up and portable-source gates only: no installer was built, signed, installed or launched, no service was registered, no swarm was joined, and no physical-matrix case ran; the pilot installer is Authenticode-unsigned'
$MinimumMajor = 10
$MinimumMinor = 0
$MinimumBuild = 19045
$RequiredPythonMinor = '3.14'

# The seven Windows source-tranche files the wave-3 lane signed off (arrival script 1.4).
$WaveThreeWindowsFiles = @(
    'tests/a13_acceptance/test_a13_windows_install_source.py',
    'tests/a13_acceptance/test_a13_windows_onboarding_invitation.py',
    'tests/a13_acceptance/test_a13_windows_onboarding_progress.py',
    'tests/a13_acceptance/test_a13_windows_package_manifest.py',
    'tests/a13_acceptance/test_a13_windows_platform_admission.py',
    'tests/a13_acceptance/test_a13_windows_service_lifecycle.py',
    'tests/a9_acceptance/test_windows_native_provider_adapter.py'
)
# Windows-lane files that landed after the arrival script was written.
$LaterWindowsFiles = @(
    'tests/a13_acceptance/test_a13_windows_join_platform_seams.py',
    'tests/a13_acceptance/test_a13_windows_acl_verification.py',
    'tests/a13_acceptance/test_a13_windows_member_plan.py',
    'tests/a13_acceptance/test_a13_windows_build.py'
)
$A13AcceptancePackage = 'tests/a13_acceptance'

$Result = [ordered]@{
    protocol       = $Protocol
    claim_boundary = $ClaimBoundary
    signed         = $false
    expected_commit = $ExpectedCommit
    started_at     = (Get-Date).ToUniversalTime().ToString('o')
    finished_at    = $null
    overall        = 'incomplete'
    steps          = [ordered]@{}
    hardware       = $null
}

function Write-ResultFile {
    $Result.finished_at = (Get-Date).ToUniversalTime().ToString('o')
    $json = $Result | ConvertTo-Json -Depth 12
    $parent = Split-Path -Parent $OutputPath
    if ($parent -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent | Out-Null
    }
    # UTF-8 without a BOM so json.load() reads it on any host.
    [System.IO.File]::WriteAllText($OutputPath, $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Set-Step {
    param([string]$Id, [string]$Status, [object]$Observed, [string]$Expected)
    $Result.steps[$Id] = [ordered]@{
        status   = $Status
        expected = $Expected
        observed = $Observed
    }
}

function Invoke-Captured {
    # Run one native command with a timeout; capture exit code, stdout and stderr.
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory = (Get-Location).Path)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = ($Arguments | ForEach-Object { ConvertTo-CommandLineArgument $_ }) -join ' '
    $info.WorkingDirectory = $WorkingDirectory
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    $process = [System.Diagnostics.Process]::Start($info)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit($StepTimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        return [ordered]@{ exit_code = $null; timed_out = $true; stdout = ''; stderr = '' }
    }
    $process.WaitForExit()
    return [ordered]@{
        exit_code = $process.ExitCode
        timed_out = $false
        stdout    = $stdoutTask.Result
        stderr    = $stderrTask.Result
    }
}

function ConvertTo-CommandLineArgument {
    # MSVC CommandLineToArgvW quoting, so every argument round-trips exactly.
    param([string]$Value)
    if ($Value -ne '' -and $Value -notmatch '[\s"]') { return $Value }
    $builder = New-Object System.Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $backslashes++; continue }
        if ($character -eq '"') {
            [void]$builder.Append('\' * ($backslashes * 2 + 1)); [void]$builder.Append('"')
        } else {
            [void]$builder.Append('\' * $backslashes); [void]$builder.Append($character)
        }
        $backslashes = 0
    }
    [void]$builder.Append('\' * ($backslashes * 2))
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Get-Tail {
    param([string]$Text, [int]$Lines = 40)
    if (-not $Text) { return '' }
    $all = $Text -split "`r?`n"
    return ($all | Select-Object -Last $Lines) -join "`n"
}

function Resolve-Python {
    if ($PythonExe) { return @{ exe = $PythonExe; prefix = @() } }
    if (Get-Command py.exe -ErrorAction SilentlyContinue) { return @{ exe = 'py.exe'; prefix = @('-3.14') } }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return @{ exe = $python.Source; prefix = @() } }
    return $null
}

function Invoke-Python {
    param([string[]]$Arguments, [string]$WorkingDirectory = $RepoPath)
    return Invoke-Captured -FilePath $Python.exe -Arguments (@($Python.prefix) + @('-B') + $Arguments) -WorkingDirectory $WorkingDirectory
}

function Invoke-PytestGate {
    # One pytest run with a junit report; counts are read from the report, not the exit code.
    param([string]$Id, [string[]]$Targets, [string]$Expected)
    $junit = Join-Path $ReportDirectory ("$Id.junit.xml")
    $run = Invoke-Python -Arguments (@('-m', 'pytest', '-q', '-p', 'no:cacheprovider', "--junitxml=$junit") + $Targets)
    $counts = [ordered]@{ tests = $null; failures = $null; errors = $null; skipped = $null; passed = $null }
    if (Test-Path -LiteralPath $junit) {
        [xml]$report = Get-Content -LiteralPath $junit -Raw
        $suites = @($report.SelectNodes('//testsuite'))
        $counts.tests = ($suites | Measure-Object -Property tests -Sum).Sum
        $counts.failures = ($suites | Measure-Object -Property failures -Sum).Sum
        $counts.errors = ($suites | Measure-Object -Property errors -Sum).Sum
        $counts.skipped = ($suites | Measure-Object -Property skipped -Sum).Sum
        $counts.passed = $counts.tests - $counts.failures - $counts.errors - $counts.skipped
    }
    $ok = ($run.exit_code -eq 0) -and ($counts.tests -gt 0) -and ($counts.failures -eq 0) -and ($counts.errors -eq 0) -and ($counts.skipped -eq 0)
    $status = 'fail'
    if ($ok) { $status = 'pass' }
    Set-Step -Id $Id -Status $status -Expected $Expected -Observed ([ordered]@{
        targets   = $Targets
        exit_code = $run.exit_code
        timed_out = $run.timed_out
        counts    = $counts
        tail      = (Get-Tail $run.stdout 60)
    })
}

function Get-HardwareSurvey {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem
    $cpus = @(Get-CimInstance -ClassName Win32_Processor | ForEach-Object {
        [ordered]@{
            name              = $_.Name.Trim()
            manufacturer      = $_.Manufacturer
            cores             = $_.NumberOfCores
            logical_processors = $_.NumberOfLogicalProcessors
            max_clock_mhz     = $_.MaxClockSpeed
            architecture_code = $_.Architecture
        }
    })
    # Win32_VideoController.AdapterRAM is a uint32 (caps at 4 GiB); the display
    # class key's qwMemorySize is the 64-bit value when the driver publishes it.
    $displayClass = 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}'
    $qwords = @{}
    if (Test-Path -LiteralPath $displayClass) {
        Get-ChildItem -LiteralPath $displayClass -ErrorAction SilentlyContinue | ForEach-Object {
            $properties = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            if ($properties -and ($properties.PSObject.Properties.Name -contains 'HardwareInformation.qwMemorySize') -and ($properties.PSObject.Properties.Name -contains 'DriverDesc')) {
                $qwords[$properties.DriverDesc] = [int64]$properties.'HardwareInformation.qwMemorySize'
            }
        }
    }
    $gpus = @(Get-CimInstance -ClassName Win32_VideoController | ForEach-Object {
        $vram = $null
        if ($qwords.ContainsKey($_.Name)) { $vram = $qwords[$_.Name] }
        [ordered]@{
            name                  = $_.Name
            driver_version        = $_.DriverVersion
            adapter_ram_uint32    = $_.AdapterRAM
            vram_bytes_qw         = $vram
            video_processor       = $_.VideoProcessor
        }
    })
    $volumes = @(Get-CimInstance -ClassName Win32_LogicalDisk -Filter 'DriveType=3' | ForEach-Object {
        [ordered]@{ drive = $_.DeviceID; file_system = $_.FileSystem; size_bytes = $_.Size; free_bytes = $_.FreeSpace }
    })
    $physical = @()
    if (Get-Command Get-PhysicalDisk -ErrorAction SilentlyContinue) {
        $physical = @(Get-PhysicalDisk | ForEach-Object {
            [ordered]@{ name = $_.FriendlyName; media_type = [string]$_.MediaType; bus_type = [string]$_.BusType; size_bytes = $_.Size }
        })
    }
    $nvidia = $null
    if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
        $query = Invoke-Captured -FilePath 'nvidia-smi.exe' -Arguments @('--query-gpu=name,memory.total,driver_version', '--format=csv,noheader')
        $nvidia = $query.stdout.Trim()
    }
    return [ordered]@{
        os = [ordered]@{
            caption         = $os.Caption
            version         = $os.Version
            build_number    = $os.BuildNumber
            os_architecture = $os.OSArchitecture
        }
        processor_architecture = $env:PROCESSOR_ARCHITECTURE
        processor_architew6432 = $env:PROCESSOR_ARCHITEW6432
        machine = [ordered]@{ manufacturer = $computer.Manufacturer; model = $computer.Model }
        memory_bytes = $computer.TotalPhysicalMemory
        cpus    = $cpus
        gpus    = $gpus
        nvidia_smi = $nvidia
        volumes = $volumes
        physical_disks = $physical
    }
}

# Python probe for 1.1 (the tranche's own admission) and 1.6 (POSIX-primitive gaps).
$ProbeSource = @'
import json, os, platform, signal, sys, tempfile
from pathlib import Path
# The probe file lives outside the checkout; import the repository from the CWD.
sys.path.insert(0, os.getcwd())
out = {"python": sys.version, "platform": sys.platform, "machine": platform.machine()}
def record(name, call):
    try:
        out[name] = {"ok": True, "value": call()}
    except BaseException as exc:
        out[name] = {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:300]}
out["attributes"] = {name: hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "getuid", "killpg", "fchmod", "setsid")}
out["signals"] = {name: hasattr(signal, name) for name in ("SIGKILL", "SIGTERM", "SIGHUP", "CTRL_BREAK_EVENT")}
def admission():
    import winreg
    from mycelium_a13_windows.platform_admission import admit_windows_platform
    keys = ("CurrentMajorVersionNumber", "CurrentMinorVersionNumber", "CurrentBuildNumber", "UBR", "ProductName", "DisplayVersion", "EditionID")
    lines = []
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
        for name in keys:
            try:
                lines.append(f"{name}={winreg.QueryValueEx(key, name)[0]}")
            except OSError:
                pass
    text = "\n".join(lines)
    return {"registry_values": text, "decision": admit_windows_platform(system=platform.system(), machine=platform.machine(), registry_values=text, service_manager="windows_service")}
record("admission", admission)
def directory_fsync():
    with tempfile.TemporaryDirectory() as directory:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return "directory_fsync_succeeded"
record("posix_directory_fsync", directory_fsync)
def real_acl_reader():
    import subprocess
    from mycelium_a13_windows.acl import read_owner_only_acl
    with tempfile.TemporaryDirectory() as directory:
        whoami = subprocess.run(["whoami"], capture_output=True, text=True, timeout=10)
        icacls = subprocess.run(["icacls", directory], capture_output=True, text=True, timeout=10)
        try:
            observed = read_owner_only_acl(directory)
        except BaseException as exc:
            observed = "refused: " + str(exc)
        return {"observation_for_fresh_temp_dir": observed, "fixture_whoami": whoami.stdout, "fixture_icacls": icacls.stdout, "fixture_path": directory}
record("real_acl_reader", real_acl_reader)
def real_directory_flush():
    from mycelium_a13_windows import onboarding
    with tempfile.TemporaryDirectory() as directory:
        onboarding._win32_flush_directory(Path(directory))
    return "flush_file_buffers_succeeded"
record("real_directory_flush", real_directory_flush)
def real_atomic_write():
    from mycelium_a13_windows import onboarding
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "member.json"
        onboarding._atomic_write(target, b"{}")
        return {"written": target.read_bytes() == b"{}", "leftover_temporary": (Path(directory) / ".member.json.joining").exists()}
record("real_atomic_write", real_atomic_write)
def import_member_runtime_modules():
    import importlib
    loaded = {}
    for name in ("mycelium_node", "mycelium_service_runner", "mycelium_a13_windows.onboarding", "mycelium_platform_neutral.windows_provider"):
        try:
            importlib.import_module(name)
            loaded[name] = "imported"
        except BaseException as exc:
            loaded[name] = type(exc).__name__ + ": " + str(exc)[:200]
    return loaded
record("imports", import_member_runtime_modules)
print(json.dumps(out, default=str))
'@

$ReportDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('mycelium-a13-bringup-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $ReportDirectory | Out-Null

try {
    # ---- hw: hardware survey (never gates the run; recorded for the owner) ----
    try {
        $Result.hardware = Get-HardwareSurvey
    } catch {
        $Result.hardware = [ordered]@{ error = $_.Exception.Message }
    }

    # ---- 1.1 OS / architecture floor ----
    $currentVersion = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
    $architecture = $env:PROCESSOR_ARCHITECTURE
    if ($env:PROCESSOR_ARCHITEW6432) { $architecture = $env:PROCESSOR_ARCHITEW6432 }
    $major = [int]$currentVersion.CurrentMajorVersionNumber
    $minor = [int]$currentVersion.CurrentMinorVersionNumber
    $build = [int]$currentVersion.CurrentBuildNumber
    $floorOk = ($major -gt $MinimumMajor -or ($major -eq $MinimumMajor -and $minor -ge $MinimumMinor)) -and ($build -ge $MinimumBuild) -and ($architecture -eq 'AMD64')
    $floorStatus = 'fail'
    if ($floorOk) { $floorStatus = 'pass' }
    Set-Step -Id '1.1_os_floor' -Status $floorStatus -Expected "Windows >= $MinimumMajor.$MinimumMinor build >= $MinimumBuild on AMD64" -Observed ([ordered]@{
        major = $major; minor = $minor; build = $build; ubr = $currentVersion.UBR
        product_name = $currentVersion.ProductName; display_version = $currentVersion.DisplayVersion
        edition_id = $currentVersion.EditionID; architecture = $architecture
    })

    # ---- 1.2 toolchain ----
    $Python = Resolve-Python
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    $toolchain = [ordered]@{ python = $null; git = $null; deps = $null; installed_deps = $false }
    if ($git) { $toolchain.git = (Invoke-Captured -FilePath $git.Source -Arguments @('--version')).stdout.Trim() }
    if ($Python) {
        $version = Invoke-Captured -FilePath $Python.exe -Arguments (@($Python.prefix) + @('-c', 'import sys; print("%d.%d.%d" % sys.version_info[:3])'))
        $toolchain.python = $version.stdout.Trim()
        if ($InstallPythonDeps) {
            $install = Invoke-Captured -FilePath $Python.exe -Arguments (@($Python.prefix) + @('-m', 'pip', 'install', '--user', 'pytest', 'cryptography'))
            $toolchain.installed_deps = ($install.exit_code -eq 0)
        }
        $deps = Invoke-Captured -FilePath $Python.exe -Arguments (@($Python.prefix) + @('-c', 'import pytest, cryptography; print(pytest.__version__, cryptography.__version__)'))
        if ($deps.exit_code -eq 0) { $toolchain.deps = $deps.stdout.Trim() }
    }
    $toolchainOk = $git -and $Python -and $toolchain.python -and $toolchain.python.StartsWith("$RequiredPythonMinor.") -and $toolchain.deps
    if (-not $toolchainOk) {
        Set-Step -Id '1.2_toolchain' -Status 'fail' -Expected "CPython $RequiredPythonMinor.x, git, pytest + cryptography" -Observed $toolchain
        throw 'toolchain_incomplete'
    }
    Set-Step -Id '1.2_toolchain' -Status 'pass' -Expected "CPython $RequiredPythonMinor.x, git, pytest + cryptography" -Observed $toolchain

    # ---- 1.3 materialise the exact commit and verify the A6 manifest ----
    # core.autocrlf=false + core.eol=lf: the A6 check hashes working-tree bytes,
    # and Git for Windows' default CRLF conversion would change every hash.
    $gitConfig = @('-c', 'core.autocrlf=false', '-c', 'core.eol=lf')
    if (-not (Test-Path -LiteralPath $RepoPath)) {
        if (-not $SourceBundle) { throw 'repo_path_missing_and_no_source_bundle' }
        $clone = Invoke-Captured -FilePath $git.Source -Arguments ($gitConfig + @('clone', '--no-checkout', $SourceBundle, $RepoPath))
        if ($clone.exit_code -ne 0) { throw ('git_clone_failed: ' + (Get-Tail $clone.stderr 5)) }
    } elseif ($SourceBundle) {
        $fetch = Invoke-Captured -FilePath $git.Source -Arguments @('-C', $RepoPath, 'fetch', $SourceBundle, '+refs/*:refs/remotes/bundle/*')
        if ($fetch.exit_code -ne 0) { throw ('git_bundle_fetch_failed: ' + (Get-Tail $fetch.stderr 5)) }
    }
    [void](Invoke-Captured -FilePath $git.Source -Arguments @('-C', $RepoPath, 'config', 'core.autocrlf', 'false'))
    [void](Invoke-Captured -FilePath $git.Source -Arguments @('-C', $RepoPath, 'config', 'core.eol', 'lf'))
    $checkout = Invoke-Captured -FilePath $git.Source -Arguments ($gitConfig + @('-C', $RepoPath, 'checkout', '--detach', $ExpectedCommit))
    $head = (Invoke-Captured -FilePath $git.Source -Arguments @('-C', $RepoPath, 'rev-parse', 'HEAD')).stdout.Trim()
    $porcelain = (Invoke-Captured -FilePath $git.Source -Arguments @('-C', $RepoPath, 'status', '--porcelain', '-uall')).stdout.Trim()
    $manifest = Invoke-Python -Arguments @('scripts/build_a6_source_manifest.py', '--base', $A6Base, '--check')
    $materialised = [ordered]@{
        checkout_exit = $checkout.exit_code
        head          = $head
        porcelain     = $porcelain
        a6_check_exit = $manifest.exit_code
        a6_check_stdout = (Get-Tail $manifest.stdout 5)
        a6_check_stderr = (Get-Tail $manifest.stderr 10)
    }
    $materialisedOk = ($head -eq $ExpectedCommit) -and (-not $porcelain) -and ($manifest.exit_code -eq 0) -and ($manifest.stdout -match 'A6 source manifest verified')
    $materialisedStatus = 'fail'
    if ($materialisedOk) { $materialisedStatus = 'pass' }
    Set-Step -Id '1.3_materialise_and_a6_check' -Status $materialisedStatus -Expected "HEAD == $ExpectedCommit, clean tree, 'A6 source manifest verified' exit 0" -Observed $materialised
    if (-not $materialisedOk) { throw 'exact_commit_not_materialised' }

    # ---- 1.4 Windows acceptance files; 1.5 the whole A13 acceptance package ----
    Invoke-PytestGate -Id '1.4a_wave3_windows_files' -Targets $WaveThreeWindowsFiles -Expected 'every test passes, zero skipped (host-independent by construction)'
    Invoke-PytestGate -Id '1.4b_later_windows_files' -Targets $LaterWindowsFiles -Expected 'every test passes, zero skipped'
    Invoke-PytestGate -Id '1.5_a13_acceptance_package' -Targets @($A13AcceptancePackage) -Expected 'every test passes, zero skipped'

    # ---- 1.6 POSIX-primitive gap probes (recorded; a probe failure is a finding) ----
    $probeFile = Join-Path $ReportDirectory 'probe.py'
    [System.IO.File]::WriteAllText($probeFile, $ProbeSource, (New-Object System.Text.UTF8Encoding($false)))
    $probe = Invoke-Python -Arguments @($probeFile)
    $probeDocument = $null
    try { $probeDocument = $probe.stdout | ConvertFrom-Json } catch { }
    $probeStatus = 'fail'
    if ($probe.exit_code -eq 0 -and $probeDocument) { $probeStatus = 'recorded' }
    Set-Step -Id '1.6_posix_primitive_probes' -Status $probeStatus -Expected 'probe ran; each primitive recorded as ok or its error (findings, not gates)' -Observed ([ordered]@{
        exit_code = $probe.exit_code
        probe     = $probeDocument
        stderr    = (Get-Tail $probe.stderr 20)
    })

    $failed = @($Result.steps.Values | Where-Object { $_.status -eq 'fail' })
    if ($failed.Count -eq 0) { $Result.overall = 'pass' } else { $Result.overall = 'fail' }
} catch {
    $Result.overall = 'fail'
    $Result['error'] = $_.Exception.Message
} finally {
    Write-ResultFile
    Remove-Item -LiteralPath $ReportDirectory -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Output ("a13 windows bring-up: overall=" + $Result.overall + " result=" + $OutputPath)
if ($Result.overall -eq 'pass') { exit 0 }
exit 1
