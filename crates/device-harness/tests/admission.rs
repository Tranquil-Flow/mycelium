// SPDX-License-Identifier: AGPL-3.0-only
// R7: device_common.py:51-57,85-103,347-374; owner AC waiver.
use device_harness::admission::*;

#[test]
fn power_owner_ac_waiver_only_changes_start_floor() {
    for (source, pct, initial, accepted) in [
        (PowerSource::Battery, Some(34), true, false),
        (PowerSource::Battery, Some(35), true, true),
        (PowerSource::Ac, Some(27), true, true),
        (PowerSource::Ac, Some(0), true, true),
        (PowerSource::Unknown, Some(27), true, false),
        (PowerSource::Ac, Some(24), false, false),
        (PowerSource::Battery, Some(24), false, false),
        (PowerSource::Battery, Some(25), false, true),
        (PowerSource::Ac, None, true, true),
        (PowerSource::Ac, None, false, true),
        (PowerSource::Unknown, None, true, false),
        (PowerSource::Battery, None, true, false),
        (PowerSource::Ac, Some(101), true, false),
    ] {
        assert_eq!(
            admit_power(
                Power {
                    source,
                    percent: pct
                },
                initial
            )
            .is_ok(),
            accepted,
            "{source:?}/{pct:?}/{initial}"
        );
    }
}

#[test]
fn android_metered_and_nwpath_are_not_unmetered() {
    assert_eq!(
        classify_network("SSID : ANDROID_METERED", None, None),
        NetworkClass::Metered
    );
    assert_eq!(
        classify_network("", Some(true), Some(false)),
        NetworkClass::Metered
    );
    assert_eq!(
        classify_network("", Some(false), Some(true)),
        NetworkClass::Constrained
    );
    assert_eq!(
        classify_network("SSID : ordinary-wifi", None, None),
        NetworkClass::Unknown
    );
    assert_eq!(
        classify_network("", Some(false), Some(false)),
        NetworkClass::Unmetered
    );
    // Positive metered evidence must win over a negative NWPath flag.
    assert_eq!(
        classify_network("ANDROID_METERED", Some(false), Some(false)),
        NetworkClass::Metered
    );
}

#[test]
fn metered_path_refuses_unmetered_only_and_unknown_fails_closed() {
    for class in [
        NetworkClass::Metered,
        NetworkClass::Constrained,
        NetworkClass::Unknown,
    ] {
        assert!(admit_network(class, NetworkConsent::UnmeteredOnly).is_err());
    }
    assert!(admit_network(NetworkClass::Unmetered, NetworkConsent::UnmeteredOnly).is_ok());
    assert!(admit_network(NetworkClass::Metered, NetworkConsent::MeteredAllowed).is_ok());
    assert!(admit_network(NetworkClass::Unknown, NetworkConsent::MeteredAllowed).is_err());
}
