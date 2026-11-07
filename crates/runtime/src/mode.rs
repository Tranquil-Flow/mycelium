// SPDX-License-Identifier: AGPL-3.0-only
use protocol::ContractError;
use serde_json::Value;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Normal,
    Fast,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ModeSource {
    Default,
    Saved,
    Environment,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Selection {
    pub mode: Mode,
    pub source: ModeSource,
    pub locked: bool,
}
/// No environment mutation; next-run selection only, not execution authority.
pub fn select(
    override_value: Option<&str>,
    saved: Option<&Value>,
) -> Result<Selection, ContractError> {
    if let Some(value) = override_value {
        let mode = match value {
            "on" => Mode::Fast,
            "off" => Mode::Normal,
            _ => return Err(ContractError("token_path_activation_invalid")),
        };
        return Ok(Selection {
            mode,
            source: ModeSource::Environment,
            locked: true,
        });
    }
    let (mode, source) = if let Some(saved) = saved {
        let object = saved
            .as_object()
            .ok_or(ContractError("fast_mode_state_invalid"))?;
        if object.len() != 2 || saved["protocol"] != "mycelium.a13_fast_mode.v1" {
            return Err(ContractError("fast_mode_state_invalid"));
        }
        let enabled = saved["enabled"]
            .as_bool()
            .ok_or(ContractError("fast_mode_state_invalid"))?;
        (
            if enabled { Mode::Fast } else { Mode::Normal },
            ModeSource::Saved,
        )
    } else {
        (Mode::Normal, ModeSource::Default)
    };
    Ok(Selection {
        mode,
        source,
        locked: false,
    })
}
