// SPDX-License-Identifier: AGPL-3.0-only
use crate::{Fault, FaultKind, Result};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PowerSource {
    Ac,
    Battery,
    Unknown,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct Power {
    pub source: PowerSource,
    pub percent: Option<u8>,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NetworkClass {
    Unmetered,
    Metered,
    Constrained,
    Unknown,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum NetworkConsent {
    UnmeteredOnly,
    MeteredAllowed,
}

pub fn admit_power(power: Power, initial: bool) -> Result<()> {
    let floor = if !initial {
        25
    } else if power.source == PowerSource::Ac {
        0
    } else {
        35
    };
    match power.percent {
        Some(pct) if pct > 100 => Err(Fault::new(
            "battery_observation_unavailable",
            FaultKind::Observation,
        )),
        None if power.source != PowerSource::Ac => Err(Fault::new(
            "battery_observation_unavailable",
            FaultKind::Observation,
        )),
        Some(pct) if pct < floor => Err(Fault::new(
            if initial {
                "battery_start_floor"
            } else {
                "battery_floor"
            },
            FaultKind::Admission,
        )),
        _ => Ok(()),
    }
}
/// Absence of a metered hint is not positive unmetered evidence. NWPath must
/// positively report both flags false; expensive/constrained always wins.
pub fn classify_network(
    summary: &str,
    expensive: Option<bool>,
    constrained: Option<bool>,
) -> NetworkClass {
    if constrained == Some(true) {
        NetworkClass::Constrained
    } else if expensive == Some(true) || summary.contains("ANDROID_METERED") {
        NetworkClass::Metered
    } else if expensive == Some(false) && constrained == Some(false) {
        NetworkClass::Unmetered
    } else {
        NetworkClass::Unknown
    }
}
pub fn admit_network(class: NetworkClass, consent: NetworkConsent) -> Result<()> {
    match (class, consent) {
        (NetworkClass::Unknown, _) => Err(Fault::new(
            "network_class_unobserved",
            FaultKind::Observation,
        )),
        (NetworkClass::Metered | NetworkClass::Constrained, NetworkConsent::UnmeteredOnly) => {
            Err(Fault::new("network_consent_mismatch", FaultKind::Admission))
        }
        _ => Ok(()),
    }
}
