// SPDX-License-Identifier: AGPL-3.0-only
//! Actual headless ownership with real seed and mailbox, no native worker.
use super::*;
struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let path =
            std::env::temp_dir().join(format!("l10-observation-{tag}-{}", std::process::id()));
        assert!(!path.exists());
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn fixture(tag: &str) -> (Root, Shared, Arc<SeedRuntime>) {
    let root = Root::new(tag);
    let core = Arc::new(Mutex::new(load_core(root.0.clone()).unwrap()));
    let seed = Arc::new(start_seed(&core.lock().unwrap().root, "swarm-lifetime", None).unwrap());
    {
        let mut c = core.lock().unwrap();
        c.running = true;
        c.seed = Some(seed.clone());
    }
    (root, core, seed)
}
#[test]
fn actual_owner_is_seed_scoped_and_old_hosts_do_not_retain_or_authorize_core() {
    let (_root, core, seed) = fixture("lifetime");
    let backend = Backend {
        core: Arc::downgrade(&core),
        published: BTreeMap::new(),
    };
    let host = backend.host().unwrap();
    let one = backend.managed(&host).unwrap();
    let two = backend.managed(&backend.host().unwrap()).unwrap();
    assert!(
        Arc::ptr_eq(&one, &two),
        "not one journal owner per run/factory"
    );
    assert!(Arc::ptr_eq(&host.signer, &seed.coordinator.shared_signer()));
    assert!(seed.managed_path.join("owner/owner.json").is_file());
    let weak = Arc::downgrade(&core);
    core.lock().unwrap().seed = None;
    assert_eq!(host.pin().err().unwrap().0, "managed_owner_closed");
    seed.close();
    drop(core);
    assert!(
        weak.upgrade().is_none(),
        "retained owner/factory must not strongly retain Core"
    );
    assert!(host.pin().is_err());
    one.close().unwrap();
}
#[test]
fn host_waiting_for_creation_cannot_publish_after_authority_stop() {
    let (_root, core, seed) = fixture("creation");
    let backend = Backend {
        core: Arc::downgrade(&core),
        published: BTreeMap::new(),
    };
    let host = backend.host().unwrap();
    let guard = seed.managed_init.lock().unwrap();
    let (started, ready) = std::sync::mpsc::sync_channel(1);
    let worker = thread::spawn(move || {
        started.send(()).unwrap();
        backend.managed(&host)
    });
    ready.recv().unwrap();
    core.lock().unwrap().running = false;
    seed.close();
    drop(guard);
    assert_eq!(
        worker.join().unwrap().err().unwrap().0,
        "managed_owner_closed"
    );
    assert!(
        !seed.managed_path.exists(),
        "no delayed owner/mailbox/endpoint publication"
    );
}

// Same managed-offer SourceArgument family; not additional census review credit.
// The long strings are protocol data, never filesystem paths opened by this test.
#[test]
fn mounted_stage_policy_counts_unicode_characters_at_all_actual_intakes() {
    let root = Root::new("path-intakes");
    fs::create_dir(&root.0).unwrap();
    let signer = Arc::new(Identity::from_secret("owner", &[41; 32]).unwrap());
    let member = Identity::from_secret("member", &[42; 32]).unwrap();
    let limit = BudgetId::PyStagePrepositionPathCharacters
        .count()
        .unwrap()
        .get() as usize;
    let ttl = BudgetId::PyFinalMyceliumA13MacosManagedOffersTtlSecondsL107C77
        .span(Entries::ZERO)
        .unwrap();
    for domain in ["source", "destination", "stage-ack", "cleanup-ack"] {
        for (label, symbol, count, accepted) in [
            ("ascii", "a", limit, true),
            ("unicode", "é", limit, true),
            ("over", "é", limit + 1, false),
        ] {
            let path = if domain == "destination" {
                symbol.repeat(count)
            } else {
                format!("/{}", symbol.repeat(count - 1))
            };
            assert_eq!(path.chars().count(), count);
            if symbol == "é" {
                assert!(path.len() > limit);
            }
            let mut mailbox = Mailbox::new_shared(
                control_runtime::Store::open_with_policy(&root.0.join(format!("{domain}-{label}.sqlite")), None,
                    BudgetId::PyMyceliumA13MacosManagedControlManagedcontrolmailboxConnectTimeoutL823.span(Entries::ZERO).unwrap()).unwrap(),
                signer.clone(), "swarm", "owner", limits().unwrap()).unwrap().with_stage_policy(policy().unwrap()).unwrap();
            let binding = json!({"target_node_id":"node-member", "target_endpoint_id":"member", "target_key_digest":member.verification_key_digest(), "membership_generation":1, "member_incarnation":"inc-1", "run_id":"run-1", "deployment_id":"deployment-1"});
            mailbox
                .bind_member(&binding, &member.public_key_record(), 3000.0)
                .unwrap();
            let mut pre = json!({"protocol":"mycelium.controller_prepositioned_member_artifacts.v1", "files":[{"content_digest":protocol::artifact_grant::range_digest(b"x"), "destination_path":"destination", "size_bytes":1, "source_path":"/source"}]});
            let is_pre = matches!(domain, "source" | "destination");
            if is_pre {
                pre["files"][0][format!("{domain}_path")] = path.clone().into();
            }
            let published = mailbox.publish_stage(
                StagePublication {
                    binding: &binding,
                    id: "stage-1",
                    archive: b"hello",
                    ttl,
                    preposition: if is_pre { Some(&pre) } else { None },
                },
                || 1000.0,
            );
            if is_pre {
                assert_eq!(
                    published.is_ok(),
                    accepted,
                    "{domain}/{label}: {published:?}"
                );
                if !accepted {
                    assert!(matches!(
                        published,
                        Err(control_runtime::Error::Refused("offer_preposition_invalid"))
                    ));
                }
                continue;
            }
            let offer = published.unwrap();
            let (kind, ack) = if domain == "stage-ack" {
                (
                    "ack",
                    json!({"protocol":"mycelium.controller_remote_stage_ack.v1", "node_id":"node-member", "staging_root":path, "archive_digest":offer["archive_digest"], "archive_size_bytes":offer["archive_bytes"]}),
                )
            } else {
                mailbox.request_stage_cleanup(&offer, || 1000.0).unwrap();
                (
                    "cleanup_ack",
                    json!({"protocol":"mycelium.controller_remote_cleanup_ack.v1", "node_id":"node-member", "staging_root":path, "removed":true}),
                )
            };
            let mut statement = binding.clone();
            for (k, value) in json!({"protocol":format!("mycelium.managed_stage_{kind}.v1"), "request_id":"path-request", "swarm_id":"swarm", "sequence":1, "nonce":"path-nonce", "issued_at":1000, "expires_at":1030, "run_id":"stage-discovery", "deployment_id":"stage-discovery", "offer_id":"stage-1", "ack":ack}).as_object().unwrap() { statement[k] = value.clone(); }
            if domain == "stage-ack" {
                statement["offset"] = 0.into();
            }
            let raw = serde_json::to_vec(&json!({"protocol":protocol::managed::SIGNED_ENVELOPE_PROTOCOL, "signature":member.sign(&statement).unwrap(), "verification_key":member.public_key_record(), "statement":statement})).unwrap();
            let result = mailbox.dispatch_stage(&raw, || 1001.0);
            assert_eq!(result.is_ok(), accepted, "{domain}/{label}: {result:?}");
            if !accepted {
                let code = if domain == "stage-ack" {
                    "offer_ack_invalid"
                } else {
                    "offer_cleanup_ack_invalid"
                };
                assert!(
                    matches!(result, Err(control_runtime::Error::Refused(actual)) if actual == code)
                );
            }
        }
    }
}
