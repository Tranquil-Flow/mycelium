# SPDX-License-Identifier: AGPL-3.0-only
"""Static lint of the on-device Windows bring-up script (arrival script Phase 1).

``scripts/a13_windows_device_bringup.ps1`` can only run on the Windows device,
and no PowerShell exists on this build host, so these tests check its
structure statically: the gate order, the exact acceptance targets (each must
exist in this tree, and every Windows acceptance file must be covered), the
floor constants against the tranche's own declaration, the LF-checkout guard
the A6 hash check needs, the hardware survey classes, the embedded Python
probe (compiled here), token balance, Windows PowerShell 5.1 compatibility,
and that it executes nothing it must not (no install, service, task, download
or dynamic evaluation).  Passing proves structure only, never a device run.
"""

from __future__ import annotations

from pathlib import Path
import re

from mycelium_a13_windows.platform_admission import ADMITTED_RELEASES

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "a13_windows_device_bringup.ps1"
A6_BASE = "3282983100595dfc8a730a06cf0cf5c750219e32"
WAVE_THREE = [
    "tests/a13_acceptance/test_a13_windows_install_source.py",
    "tests/a13_acceptance/test_a13_windows_onboarding_invitation.py",
    "tests/a13_acceptance/test_a13_windows_onboarding_progress.py",
    "tests/a13_acceptance/test_a13_windows_package_manifest.py",
    "tests/a13_acceptance/test_a13_windows_platform_admission.py",
    "tests/a13_acceptance/test_a13_windows_service_lifecycle.py",
    "tests/a9_acceptance/test_windows_native_provider_adapter.py",
]


def _text() -> str:
    return SCRIPT.read_text("utf-8")


def _array(name: str) -> list[str]:
    match = re.search(r"\$" + name + r" = @\((.*?)\n\)", _text(), re.S)
    assert match, name
    return re.findall(r"'([^']+)'", match.group(1))


def _code_only(text: str) -> str:
    """Strip comments, here-strings and string literals (PowerShell lexing rules)."""

    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text.startswith("<#", index):
            end = text.index("#>", index + 2)
            index = end + 2
            continue
        if text.startswith("@'\n", index) or text.startswith("@'\r\n", index):
            end = text.index("\n'@", index)
            out.append("''")
            index = end + 3
            continue
        if text.startswith('@"\n', index) or text.startswith('@"\r\n', index):
            end = text.index('\n"@', index)
            out.append('""')
            index = end + 3
            continue
        character = text[index]
        if character == "#":
            end = text.find("\n", index)
            index = length if end < 0 else end
            continue
        if character == "'":
            index += 1
            while True:
                if text[index] == "'" and text[index + 1 : index + 2] == "'":
                    index += 2
                    continue
                if text[index] == "'":
                    break
                index += 1
            index += 1
            out.append("''")
            continue
        if character == '"':
            index += 1
            while text[index] != '"':
                index += 2 if text[index] == "`" else 1
            index += 1
            out.append('""')
            continue
        out.append(character)
        index += 1
    return "".join(out)


def test_the_script_is_ascii_and_declares_a_strict_parameter_block() -> None:
    raw = SCRIPT.read_bytes()
    # Windows PowerShell 5.1 reads a BOM-less script as ANSI: keep it ASCII.
    assert all(byte < 0x80 for byte in raw)
    text = _text()
    assert text.startswith("# SPDX-License-Identifier: AGPL-3.0-only\n")
    assert "[CmdletBinding()]" in text
    for name in ("RepoPath", "ExpectedCommit", "OutputPath"):
        assert re.search(r"\[Parameter\(Mandatory = \$true\)\](\[(?:[^\]']|'[^']*')+\])*\[string\]\$" + name, text), name
    assert "Set-StrictMode -Version Latest" in text
    assert "$ErrorActionPreference = 'Stop'" in text
    assert f"[string]$A6Base = '{A6_BASE}'" in text


def test_the_gates_run_in_arrival_script_order() -> None:
    text = _text()
    order = [
        "'1.1_os_floor'",
        "'1.2_toolchain'",
        "'1.3_materialise_and_a6_check'",
        "'1.4a_wave3_windows_files'",
        "'1.4b_later_windows_files'",
        "'1.5_a13_acceptance_package'",
        "'1.6_posix_primitive_probes'",
    ]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert text.index("Get-HardwareSurvey\n") < positions[0]
    assert "'scripts/build_a6_source_manifest.py', '--base', $A6Base, '--check'" in text
    assert "$ExpectedCommit" in text and "'rev-parse', 'HEAD'" in text
    assert "'status', '--porcelain', '-uall'" in text


def test_the_acceptance_targets_exist_and_cover_every_windows_file() -> None:
    wave_three = _array("WaveThreeWindowsFiles")
    later = _array("LaterWindowsFiles")
    assert wave_three == WAVE_THREE
    for relative in wave_three + later:
        assert (ROOT / relative).is_file(), relative
    windows_files = {
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests" / "a13_acceptance").glob("test_a13_windows_*.py")
    }
    covered = set(wave_three) | set(later)
    # This lint file is static and host-independent; it rides in 1.5 only.
    windows_files.discard("tests/a13_acceptance/test_a13_windows_device_bringup_script.py")
    assert windows_files <= covered, sorted(windows_files - covered)
    assert "$A13AcceptancePackage = 'tests/a13_acceptance'" in _text()


def test_the_floor_matches_the_tranche_declaration() -> None:
    declared = ADMITTED_RELEASES["windows"]
    major, minor = declared["minimum_version"].split(".")
    text = _text()
    assert f"$MinimumMajor = {int(major)}" in text
    assert f"$MinimumMinor = {int(minor)}" in text
    assert f"$MinimumBuild = {int(declared['minimum_build'])}" in text
    assert "-eq 'AMD64'" in text
    assert "$RequiredPythonMinor = '3.14'" in text
    # The tranche's own admission is evaluated on the observed registry values.
    assert "admit_windows_platform(" in text


def test_the_checkout_keeps_lf_bytes_for_the_a6_hash() -> None:
    text = _text()
    assert "'core.autocrlf=false'" in text and "'core.eol=lf'" in text
    assert "'config', 'core.autocrlf', 'false'" in text
    assert "'--force'" not in text and "reset --hard" not in text


def test_pytest_gates_read_counts_from_the_report_and_refuse_skips() -> None:
    text = _text()
    assert "--junitxml=" in text
    assert "($counts.skipped -eq 0)" in text
    assert "($counts.failures -eq 0)" in text and "($counts.errors -eq 0)" in text
    assert "($counts.tests -gt 0)" in text


def test_the_hardware_survey_uses_cim_for_every_required_fact() -> None:
    text = _text()
    for cim_class in (
        "Win32_OperatingSystem",
        "Win32_ComputerSystem",
        "Win32_Processor",
        "Win32_VideoController",
        "Win32_LogicalDisk",
    ):
        assert f"Get-CimInstance -ClassName {cim_class}" in text, cim_class
    for fact in (
        "NumberOfCores",
        "NumberOfLogicalProcessors",
        "TotalPhysicalMemory",
        "AdapterRAM",
        "HardwareInformation.qwMemorySize",
        "BuildNumber",
        "OSArchitecture",
        "FreeSpace",
    ):
        assert fact in text, fact


def test_the_embedded_probe_is_valid_python_covering_every_posix_gap() -> None:
    text = _text()
    match = re.search(r"\$ProbeSource = @'\n(.*?)\n'@", text, re.S)
    assert match
    source = match.group(1)
    compile(source, "probe.py", "exec")
    for probe in (
        '"O_NOFOLLOW"',
        '"getuid"',
        '"killpg"',
        "posix_directory_fsync",
        "real_acl_reader",
        "fixture_icacls",
        "fixture_whoami",
        "real_directory_flush",
        "real_atomic_write",
        "mycelium_node",
    ):
        assert probe in source, probe


def test_the_script_is_balanced_and_windows_powershell_5_compatible() -> None:
    code = _code_only(_text())
    for opening, closing in ("{}", "()", "[]"):
        assert code.count(opening) == code.count(closing), opening
    # PowerShell 7-only syntax would fail on a stock Windows 10/11 host.
    for forbidden in ("??", "?.", "&&", "||", "-AsHashtable", "-Parallel"):
        assert forbidden not in code, forbidden
    assert "WriteAllText($OutputPath, $json, (New-Object System.Text.UTF8Encoding($false)))" in _text()


def test_the_script_never_installs_registers_downloads_or_claims_a_signature() -> None:
    text = _text()
    code = _code_only(text).lower()
    for forbidden in (
        "invoke-expression",
        "iex ",
        "start-service",
        "new-service",
        "sc.exe",
        "schtasks",
        "register-scheduledtask",
        "set-executionpolicy",
        "invoke-webrequest",
        "invoke-restmethod",
        "start-bitstransfer",
        "msiexec",
    ):
        assert forbidden not in code, forbidden
    lowered = text.lower()
    for forbidden in (".hermes", "secrets/", "authenticode_subject", "signed = $true"):
        assert forbidden not in lowered, forbidden
    assert "signed         = $false" in text
    assert "Authenticode-unsigned" in text
    # The only deletion is the script's own temporary report directory.
    assert re.findall(r"Remove-Item[^\n]*", text) == [
        "Remove-Item -LiteralPath $ReportDirectory -Recurse -Force -ErrorAction SilentlyContinue"
    ]
    # pip only runs when the operator opts in.
    assert text.count("'pip', 'install'") == 1
    assert "if ($InstallPythonDeps) {" in text
