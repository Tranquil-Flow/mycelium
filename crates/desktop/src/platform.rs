// SPDX-License-Identifier: AGPL-3.0-only
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PlatformError {
    UnsupportedPlatform,
    UnsupportedArchitecture,
    UnsupportedOs,
    Probe,
}
/// Preserve app.py:84-90,124-128 @ f4e366e98a1f705dfaf182897044500d290dbd7f.
/// 15.6.1 is an EXACT exception, not a general >=15.6.1 support promise.
pub fn admit(os: &str, arch: &str, version: &str) -> Result<(), PlatformError> {
    if os != "macos" {
        return Err(PlatformError::UnsupportedPlatform);
    }
    if arch != "aarch64" {
        return Err(PlatformError::UnsupportedArchitecture);
    }
    if version == "15.6.1" {
        return Ok(());
    }
    let parts: Vec<_> = version.split('.').collect();
    if !(2..=3).contains(&parts.len())
        || parts
            .iter()
            .any(|p| p.is_empty() || p.len() > 3 || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(PlatformError::UnsupportedOs);
    }
    match parts[0].parse::<u16>() {
        Ok(major) if major >= 26 => Ok(()),
        _ => Err(PlatformError::UnsupportedOs),
    }
}
/// `sw_vers -productVersion` (`platform.mac_ver()[0]`).
pub fn host_version() -> Result<String, PlatformError> {
    let result = std::process::Command::new("/usr/bin/sw_vers")
        .arg("-productVersion")
        .output()
        .map_err(|_| PlatformError::Probe)?;
    if !result.status.success() {
        return Err(PlatformError::Probe);
    }
    let version = std::str::from_utf8(&result.stdout).map_err(|_| PlatformError::Probe)?;
    Ok(version.trim().to_string())
}
pub fn probe() -> Result<(), PlatformError> {
    admit(
        std::env::consts::OS,
        std::env::consts::ARCH,
        &host_version()?,
    )
}
