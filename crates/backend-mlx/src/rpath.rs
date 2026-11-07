// SPDX-License-Identifier: AGPL-3.0-only
//! Relocatable runtime-search-path policy for the pinned MLX dylibs.
//!
//! The dylibs install as `@rpath/<name>`, so every executable that links `libmlxc` must carry
//! an `LC_RPATH` that resolves them. A release app bundle keeps them in `Contents/Frameworks`
//! next to `Contents/MacOS/<exe>`; development and test binaries use the native install prefix.
//! This file is `#[path]`-included by `build.rs`, so it must stay std-only and warning-free
//! in both places.

/// Search path for a shipped bundle: `<App>.app/Contents/MacOS/<exe>` -> `Contents/Frameworks`.
pub const BUNDLE_RPATH: &str = "@executable_path/../Frameworks";

/// Linker arguments for the bundle rpath followed by the development install directory.
///
/// The development directory must be absolute and free of characters the `-Wl,` list syntax
/// would split or reinterpret.
pub fn link_args(dev_lib_dir: &str) -> Result<Vec<String>, String> {
    if !dev_lib_dir.starts_with('/') {
        return Err(format!(
            "BLOCKED: dev rpath must be absolute: {dev_lib_dir}"
        ));
    }
    if dev_lib_dir.chars().any(|c| c == ',' || c.is_control()) {
        return Err("BLOCKED: dev rpath contains ',' or a control character".into());
    }
    Ok([BUNDLE_RPATH, dev_lib_dir]
        .into_iter()
        .map(|p| format!("-Wl,-rpath,{p}"))
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    const MH_MAGIC_64: u32 = 0xfeed_facf;
    const LC_RPATH: u32 = 0x8000_001c;

    /// `LC_RPATH` strings of a thin little-endian 64-bit Mach-O image.
    fn macho_rpaths(image: &[u8]) -> Result<Vec<String>, String> {
        let word = |at: usize| -> Result<u32, String> {
            let bytes = image
                .get(at..at + 4)
                .ok_or_else(|| "truncated Mach-O".to_string())?;
            Ok(u32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
        };
        if word(0)? != MH_MAGIC_64 {
            return Err("not a thin little-endian 64-bit Mach-O".into());
        }
        let (count, mut at) = (word(16)?, 32usize);
        let mut paths = Vec::new();
        for _ in 0..count {
            let (cmd, size) = (word(at)?, word(at + 4)? as usize);
            if size < 8 {
                return Err("malformed load command".into());
            }
            if cmd == LC_RPATH {
                let offset = word(at + 8)? as usize;
                let raw = image
                    .get(at + offset..at + size)
                    .ok_or_else(|| "rpath outside its command".to_string())?;
                let end = raw.iter().position(|b| *b == 0).unwrap_or(raw.len());
                paths.push(String::from_utf8_lossy(&raw[..end]).into_owned());
            }
            at += size;
        }
        Ok(paths)
    }

    #[test]
    fn link_args_name_bundle_first_then_dev_dir() {
        assert_eq!(
            link_args("/opt/native/lib").unwrap(),
            vec![
                "-Wl,-rpath,@executable_path/../Frameworks".to_string(),
                "-Wl,-rpath,/opt/native/lib".to_string(),
            ]
        );
    }

    #[test]
    fn link_args_refuse_relative_or_list_splitting_dev_dirs() {
        for bad in ["", "lib", "../lib", "/a,b", "/a\nb"] {
            assert!(link_args(bad).is_err(), "{bad:?} must be refused");
        }
    }

    #[test]
    fn macho_parser_reads_synthetic_image_and_refuses_junk() {
        let mut image = Vec::new();
        for word in [MH_MAGIC_64, 0x0100_000c, 0, 6, 1, 0, 0, 0] {
            image.extend(word.to_le_bytes());
        }
        let mut path = b"@executable_path/../Frameworks\0".to_vec();
        path.resize(path.len().next_multiple_of(8), 0);
        for word in [LC_RPATH, 12 + path.len() as u32, 12] {
            image.extend(word.to_le_bytes());
        }
        image.extend(path);
        assert_eq!(
            macho_rpaths(&image).unwrap(),
            vec!["@executable_path/../Frameworks".to_string()]
        );
        assert!(macho_rpaths(&image[..40]).is_err());
        assert!(macho_rpaths(b"not macho at all, not at all..........").is_err());
    }

    #[cfg(feature = "metal")]
    #[test]
    fn linked_test_binary_carries_bundle_and_dev_rpaths() {
        let image = std::fs::read(std::env::current_exe().unwrap()).unwrap();
        // Universal images are not produced by cargo; a fat binary would fail the parser loudly.
        let found = macho_rpaths(&image).unwrap();
        let dev = env!("MYCELIUM_MLX_DEV_LIB_DIR");
        assert!(found.iter().any(|p| p == BUNDLE_RPATH), "{found:?}");
        assert!(found.iter().any(|p| p == dev), "{found:?}");
        let order = |needle: &str| found.iter().position(|p| p == needle).unwrap();
        assert!(
            order(BUNDLE_RPATH) < order(dev),
            "bundle path resolves first"
        );
    }
}
