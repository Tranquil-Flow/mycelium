// SPDX-License-Identifier: AGPL-3.0-only
use protocol::{canonical, execution_graph};
use serde_json::Value;

// O:contract:mycelium.execution_graph.v1
#[test]
fn execution_graph_parser_and_semantics_match_oracle() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("../fixtures/expanded-graph-vectors.json")).unwrap();
    for row in rows {
        let base = &row["value"];
        let graph = execution_graph::parse(base).unwrap();
        assert_eq!(
            canonical::to_bytes(graph.as_value()).unwrap(),
            row["canonical_utf8"].as_str().unwrap().as_bytes()
        );
        for (i, c) in row["cases"].as_array().unwrap().iter().enumerate() {
            let mut v = base.clone();
            let path = c["path"].as_array().unwrap();
            if path.is_empty() {
                v = c["replacement"].clone();
            } else {
                let parent = path[..path.len() - 1]
                    .iter()
                    .map(|p| {
                        format!(
                            "/{}",
                            p.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| p.to_string())
                        )
                    })
                    .collect::<String>();
                let at = v.pointer_mut(&parent).unwrap();
                let key = path.last().unwrap();
                if c["delete"] == true {
                    if let Some(s) = key.as_str() {
                        at.as_object_mut().unwrap().remove(s).unwrap();
                    } else {
                        at.as_array_mut()
                            .unwrap()
                            .remove(key.as_u64().unwrap() as usize);
                    }
                } else if let Some(s) = key.as_str() {
                    at[s] = c["replacement"].clone();
                } else {
                    at[key.as_u64().unwrap() as usize] = c["replacement"].clone();
                }
            }
            let actual = execution_graph::parse(&v);
            assert_eq!(
                actual.is_ok(),
                c["accepted"] == true,
                "case {i}: {c}; actual {actual:?}"
            );
            match actual {
                Ok(g) => assert_eq!(g.as_value(), &c["output"], "case {i}: {c}"),
                Err(e) if c["exception"] == "ContractError" => {
                    assert_eq!(e.0, c["error"].as_str().unwrap(), "case {i}: {c}")
                }
                Err(e) => assert!(!e.0.is_empty(), "case {i}: refusal must retain a code"),
            }
        }
    }
}

/// Raw JSON integer `-0` (F1 of the l1b-m16-path-builder review, CL-L1-07). CPython's
/// `json.loads` reads it as int(0), so `-0`, `0` and `false` are one value and one hash: a `-0`
/// id repeats a `0` or `false` id, and `-0` is a zero KV cost, epoch, version or layer start.
/// `-0.0` stays a float with its sign bit. `json.dumps` prints int 0 as `0`, so every document
/// here is raw text: two templates filled with raw lexemes, exactly as the read-only oracle
/// probe builds them, `$MYCELIUM_PROGRAM_ROOT/status/moonweaver/workers/l1b-m16-path-builder-a2/
/// evidence/raw_negative_zero_probe.py` (CPython 3.14.2, oracle f4e366e9: `json.loads`, then
/// `execution_graph_from_dict`, serialization.py:20-86, validation.py:12-178). Its output,
/// `raw-negative-zero-oracle.json` beside it, records each text's sha256 (asserted below) and the
/// oracle's result: the sha256 of the canonical bytes `json.dumps(execution_graph_to_dict(g),
/// sort_keys=True, separators=(",", ":"), ensure_ascii=False)` (the `canonical_utf8` form of the
/// vectors above) and the float bits of one field, or the refusal with the line that raises it.
/// Every `ContractError` is raised at validation.py:24 (`_require`) from the check cited per row;
/// the `OverflowError` is validation.py:34 (`end - start`, an int too large for a float minus the
/// float `-0.0`), which the parser refuses with a code of its own.
// O:contract:mycelium.execution_graph.v1
#[test]
fn execution_graph_raw_negative_zero_integer_parity() {
    #[derive(Debug)]
    enum Expect {
        /// Parsed: the canonical bytes' sha256 and, where given, a float field's bits.
        Graph(&'static str, Option<(&'static str, u64)>),
        /// Refused with this `ContractError` code.
        Refused(&'static str),
        /// The oracle raised this non-`ContractError` exception: any refusal code.
        Raised(&'static str),
    }
    use Expect::{Graph, Raised, Refused};
    const ONE_STAGE: &str = concat!(
        r#"{"protocol":"mycelium.execution_graph.v1","deployment_id":@DID@,"#,
        r#""deployment_epoch":@EPOCH@,"topology_version":@TOPO@,"model_id":"model-m16","#,
        r#""resolved_commit":"commit-m16","manifest_digest":"digest-m16","#,
        r#""entry_stage_id":@ENTRY@,"final_stage_id":@FINAL@,"hidden_size":@HIDDEN@,"#,
        r#""activation_bytes":@ACT@,"token_envelope_bytes":@ENV@,"stages":[{"stage_id":@S0@,"#,
        r#""range":{"start_layer":@START@,"end_layer_exclusive":@END@,"layer_count":@COUNT@},"#,
        r#""component_roles":["decoder"],"#,
        r#""stage_cost":{"prefill_work_units_per_prompt_token":@PREFILL@,"#,
        r#""decode_work_units_per_token":@DECODE@,"kv_bytes_per_context_token":@KV@},"#,
        r#""placements":[{"placement_id":@P@,"node_id":@NODE@,"replica_group_id":"group-0","#,
        r#""assignment_id":"assignment-0-0","stage_signature":"signature-0","#,
        r#""load_proof_digest":"proof-0-0","runtime_backend":"numpy","#,
        r#""runtime_endpoint":"local://0-0"},{"placement_id":@Q@,"node_id":"B","#,
        r#""replica_group_id":"group-0","assignment_id":"assignment-0-1","#,
        r#""stage_signature":"signature-0","load_proof_digest":"proof-0-1","#,
        r#""runtime_backend":"numpy","runtime_endpoint":"local://0-1"}]}],"edges":[],"#,
        r#""loopback_edges":[{"edge_id":@L0@,"from_placement_id":@P@,"to_placement_id":@P@,"#,
        r#""link_id":"link-loop-0"},{"edge_id":@L1@,"from_placement_id":@Q@,"#,
        r#""to_placement_id":@Q@,"link_id":"link-loop-1"}]}"#,
    );
    const TWO_STAGE: &str = concat!(
        r#"{"protocol":"mycelium.execution_graph.v1","deployment_id":"deployment-m16","#,
        r#""deployment_epoch":1,"topology_version":1,"model_id":"model-m16","#,
        r#""resolved_commit":"commit-m16","manifest_digest":"digest-m16","#,
        r#""entry_stage_id":@ENTRY@,"final_stage_id":@FINAL@,"hidden_size":8,"#,
        r#""activation_bytes":2,"token_envelope_bytes":16,"stages":[{"stage_id":@S0@,"#,
        r#""range":{"start_layer":0,"end_layer_exclusive":1,"layer_count":1},"#,
        r#""component_roles":["decoder"],"stage_cost":{"prefill_work_units_per_prompt_token":2,"#,
        r#""decode_work_units_per_token":1,"kv_bytes_per_context_token":0},"#,
        r#""placements":[{"placement_id":@A@,"node_id":"A","replica_group_id":"group-0","#,
        r#""assignment_id":"assignment-0-0","stage_signature":"signature-0","#,
        r#""load_proof_digest":"proof-0-0","runtime_backend":"numpy","#,
        r#""runtime_endpoint":"local://0-0"}]},{"stage_id":@S1@,"range":{"start_layer":1,"#,
        r#""end_layer_exclusive":2,"layer_count":1},"component_roles":["decoder"],"#,
        r#""stage_cost":{"prefill_work_units_per_prompt_token":2,"#,
        r#""decode_work_units_per_token":1,"kv_bytes_per_context_token":0},"#,
        r#""placements":[{"placement_id":@B@,"node_id":"B","replica_group_id":"group-1","#,
        r#""assignment_id":"assignment-1-0","stage_signature":"signature-1","#,
        r#""load_proof_digest":"proof-1-0","runtime_backend":"numpy","#,
        r#""runtime_endpoint":"local://1-0"}]}],"edges":[{"edge_id":@E0@,"#,
        r#""from_placement_id":@E0FROM@,"to_placement_id":@E0TO@,"link_id":"link-0"}@EXTRA@],"#,
        r#""loopback_edges":[{"edge_id":@L0@,"from_placement_id":@L0FROM@,"#,
        r#""to_placement_id":@L0TO@,"link_id":"link-loop-0"}]}"#,
    );
    let one_defaults: [(&str, &str); 20] = [
        ("@DID@", r#""deployment-m16""#),
        ("@EPOCH@", "1"),
        ("@TOPO@", "1"),
        ("@ENTRY@", r#""stage-0""#),
        ("@FINAL@", r#""stage-0""#),
        ("@HIDDEN@", "8"),
        ("@ACT@", "2"),
        ("@ENV@", "16"),
        ("@S0@", r#""stage-0""#),
        ("@START@", "0"),
        ("@END@", "1"),
        ("@COUNT@", "1"),
        ("@PREFILL@", "2"),
        ("@DECODE@", "1"),
        ("@KV@", "0"),
        ("@P@", r#""p""#),
        ("@Q@", r#""q""#),
        ("@NODE@", r#""A""#),
        ("@L0@", r#""loop-0""#),
        ("@L1@", r#""loop-1""#),
    ];
    let two_defaults: [(&str, &str); 13] = [
        ("@ENTRY@", r#""stage-0""#),
        ("@FINAL@", r#""stage-1""#),
        ("@S0@", r#""stage-0""#),
        ("@S1@", r#""stage-1""#),
        ("@A@", r#""a""#),
        ("@B@", r#""b""#),
        ("@E0@", r#""edge-0""#),
        ("@E0FROM@", r#""a""#),
        ("@E0TO@", r#""b""#),
        ("@EXTRA@", ""),
        ("@L0@", r#""loop-0""#),
        ("@L0FROM@", r#""b""#),
        ("@L0TO@", r#""a""#),
    ];
    // 10**400: an exact int, too large for a float.
    let huge = format!("1{}", "0".repeat(400));
    let huge = huge.as_str();
    /// (name, template, raw substitutions, sha256 of the filled text, the oracle's result)
    type Case<'a> = (&'a str, &'a str, Vec<(&'a str, &'a str)>, &'a str, Expect);
    let cases: Vec<Case<'_>> = vec![
        (
            "zero-control",
            "one",
            vec![],
            "sha256:2337201e08ec1f7cd60c6138219b9a6ab40a48368322ab1646f746feb4d968ce",
            Graph(
                "sha256:818721d4f4458fced3cbd427a3ddba133b3912538c411c37b5fdea3414b853c6",
                None,
            ),
        ),
        (
            "kv-minus-zero",
            "one",
            vec![("@KV@", "-0")],
            "sha256:4476312730f8c85d8f3b5e72fc30af184f135c3eb94732114da8adb81027aad3",
            Graph(
                "sha256:818721d4f4458fced3cbd427a3ddba133b3912538c411c37b5fdea3414b853c6",
                None,
            ),
        ),
        (
            "kv-float-minus-zero",
            "one",
            vec![("@KV@", "-0.0")],
            "sha256:dfba636e2a191dd362d86fa8256319b07ff76451b70eae595f87f930f08e6859",
            Graph(
                "sha256:507507a9c341346269d406ff32a54e90a12b673b7624947c1212a05e167cda50",
                Some((
                    "/stages/0/stage_cost/kv_bytes_per_context_token",
                    0x8000000000000000,
                )),
            ),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-minus-zero-then-zero",
            "one",
            vec![("@P@", "-0"), ("@Q@", "0")],
            "sha256:c4fc0a044f4414a42815e27dad4cb84f6d006db13b6733e349d39096f9d873bc",
            Refused("duplicate_placement_id"),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-zero-then-minus-zero",
            "one",
            vec![("@P@", "0"), ("@Q@", "-0")],
            "sha256:c82936ea39ebec0d2e6ce7625ecd0d0e89ae96966432731e739478a630b29444",
            Refused("duplicate_placement_id"),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-minus-zero-then-false",
            "one",
            vec![("@P@", "-0"), ("@Q@", "false")],
            "sha256:071d2b5396578f1db7d02c4a9dd82de5e3dbcebbea2c54e1e205d81973400f8a",
            Refused("duplicate_placement_id"),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-false-then-minus-zero",
            "one",
            vec![("@P@", "false"), ("@Q@", "-0")],
            "sha256:02b65e8cb534eeb53c196adbd802eed272438d50fcae6ee03b26059f15f4fcd5",
            Refused("duplicate_placement_id"),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-minus-zero-then-float-zero",
            "one",
            vec![("@P@", "-0"), ("@Q@", "0.0")],
            "sha256:81a4c5fc2ccab09983424ad75765792d1c1cd842ddfecf2c2f9f2c8492b2ba84",
            Refused("duplicate_placement_id"),
        ),
        // duplicate_placement_id: mycelium_router/validation.py:24 from :84
        (
            "placement-float-minus-zero-then-zero",
            "one",
            vec![("@P@", "-0.0"), ("@Q@", "0")],
            "sha256:cf8d80bb2c06a7385116832aeafdaa31f42a03c3423ecd1aa7fe097a0be019a4",
            Refused("duplicate_placement_id"),
        ),
        (
            "placement-minus-zero-then-one",
            "one",
            vec![("@P@", "-0"), ("@Q@", "1")],
            "sha256:33dc387fc061df37587aac869570fc477c42a54c8e65f1b8338dfb61df518cbf",
            Graph(
                "sha256:c3c17ccac97729935123dfb8b092e485e7afffd840325f6e151af8eae375342d",
                None,
            ),
        ),
        // duplicate_edge_id: mycelium_router/validation.py:24 from :159
        (
            "loopback-id-minus-zero-then-zero",
            "one",
            vec![("@L0@", "-0"), ("@L1@", "0")],
            "sha256:94916d27abd7d3e911e4876ccab368c25d533d795aa7ff4697b30dd30ec35458",
            Refused("duplicate_edge_id"),
        ),
        (
            "entry-final-stage-minus-zero-false",
            "one",
            vec![("@S0@", "0"), ("@ENTRY@", "-0"), ("@FINAL@", "false")],
            "sha256:d6f4e797a7cbde0d795037946e05e58a510e3c92852ae3c7146469cec728ac66",
            Graph(
                "sha256:ca7f00914fed2b124f6bfb0cefdf884fcf0c6b9fe0776bb7a56111bb253778db",
                None,
            ),
        ),
        (
            "deployment-epoch-minus-zero",
            "one",
            vec![("@EPOCH@", "-0")],
            "sha256:0575971766f5cbd4560e9d077073128676320804e24b6bed49440658244b6220",
            Graph(
                "sha256:7ec641e44f68a77923359fe05d5c16ce7c3804f7fb3f9a81d8bd281fe894bf6f",
                None,
            ),
        ),
        (
            "topology-version-minus-zero",
            "one",
            vec![("@TOPO@", "-0")],
            "sha256:98085bc4a850ce91640184506ea5b11f978d0db2a4d531e3fb0685329d9f4337",
            Graph(
                "sha256:3ef078eecc6b943d7784a94156a4fa866a8c57d50e80592a167dfaebe43da8c0",
                None,
            ),
        ),
        (
            "token-envelope-bytes-minus-zero",
            "one",
            vec![("@ENV@", "-0")],
            "sha256:c449a67906269a67f38352d3095503026609d7e5f9760bf71e49f273ac8846c3",
            Graph(
                "sha256:8b972bab436e054c4c53365db6d6d90849910fc2ce49aa863f18929a02a01624",
                None,
            ),
        ),
        // invalid_topology_version: mycelium_router/validation.py:24 from :45
        (
            "epoch-minus-zero-then-topology-minus-one",
            "one",
            vec![("@EPOCH@", "-0"), ("@TOPO@", "-1")],
            "sha256:c67ebfab5ad59fc97c1365b6b36c93accdf7b234fa5739e92598735687a4e2a2",
            Refused("invalid_topology_version"),
        ),
        (
            "start-layer-minus-zero",
            "one",
            vec![("@START@", "-0")],
            "sha256:2fe1f8ac03d9a3b03bbc1d52fe9fd4330c7cfb8513b26a33323be56723c90770",
            Graph(
                "sha256:818721d4f4458fced3cbd427a3ddba133b3912538c411c37b5fdea3414b853c6",
                None,
            ),
        ),
        (
            "start-layer-minus-zero-end-ten",
            "one",
            vec![("@START@", "-0"), ("@END@", "10"), ("@COUNT@", "10")],
            "sha256:bdda57c1589d08d87fb3742b7dd8772dbd453ab0a07587aee741942f7ddf0ffe",
            Graph(
                "sha256:d169198390ce39de7f2658719374750f5fe24df6c0438423bccfbaec7285ab08",
                None,
            ),
        ),
        (
            "start-layer-minus-zero-end-huge",
            "one",
            vec![("@START@", "-0"), ("@END@", huge), ("@COUNT@", huge)],
            "sha256:1cc0a00808df250687103d227951df17fb54677b52780eb71acfebb04da6cbd4",
            Graph(
                "sha256:49c786e05c2920ef940feef59b2245ecf67518dbe0fbfa9d504ed7e4813c6996",
                None,
            ),
        ),
        (
            "start-layer-float-minus-zero",
            "one",
            vec![("@START@", "-0.0")],
            "sha256:e3f94851424eb0c153aaacdbff2b0d3304cc2d4dcdf2bea8a406b825eabf119f",
            Graph(
                "sha256:25320cd1bb410e8e9d4e5bfe345206699e08333d5d241c9811de36b341a91977",
                Some(("/stages/0/range/start_layer", 0x8000000000000000)),
            ),
        ),
        // OverflowError (int too large to convert to float): mycelium_router/validation.py:34
        (
            "start-layer-float-minus-zero-end-huge",
            "one",
            vec![("@START@", "-0.0"), ("@END@", huge), ("@COUNT@", huge)],
            "sha256:79b04398c98229f3059f94973d5e65977b9ec3441d0231db4a75a55dbbc7cbcb",
            Raised("OverflowError"),
        ),
        // empty_or_reversed_layer_range: mycelium_router/validation.py:24 from :29
        (
            "end-layer-minus-zero",
            "one",
            vec![("@END@", "-0")],
            "sha256:ad99017764b950388db0814bfede823febd707cb8affe20ce7b9b4bf5d528f9d",
            Refused("empty_or_reversed_layer_range"),
        ),
        // range_count_mismatch: mycelium_router/validation.py:24 from :33
        (
            "layer-count-minus-zero",
            "one",
            vec![("@COUNT@", "-0")],
            "sha256:b568b576850cbe1bbbd69a8e53f8c6be73e92c4f64f579bd12268a0e3904bb8e",
            Refused("range_count_mismatch"),
        ),
        // invalid_hidden_size: mycelium_router/validation.py:24 from :48
        (
            "hidden-size-minus-zero",
            "one",
            vec![("@HIDDEN@", "-0")],
            "sha256:16894fd852709f4f707813910c0313bcac6fc305230f834c181097f296ced290",
            Refused("invalid_hidden_size"),
        ),
        // invalid_activation_bytes: mycelium_router/validation.py:24 from :49
        (
            "activation-bytes-minus-zero",
            "one",
            vec![("@ACT@", "-0")],
            "sha256:5b4dda6e5ce3879eee5f8c7a920100c2380070397cb2fda846b17b843f02982d",
            Refused("invalid_activation_bytes"),
        ),
        // invalid_prefill_cost: mycelium_router/validation.py:24 from :61
        (
            "prefill-cost-minus-zero",
            "one",
            vec![("@PREFILL@", "-0")],
            "sha256:b3879c78e8a7a69cb53b3bbb4b88bd82be6c13fbbdbc6ae4c13ae829f0b11836",
            Refused("invalid_prefill_cost"),
        ),
        // invalid_decode_cost: mycelium_router/validation.py:24 from :66
        (
            "decode-cost-minus-zero",
            "one",
            vec![("@DECODE@", "-0")],
            "sha256:46a75b0fca688d830625935736a6541c9aa9cf216337d334ba7b5c7d9230f447",
            Refused("invalid_decode_cost"),
        ),
        // missing_deployment_id: mycelium_router/validation.py:24 from :43
        (
            "deployment-id-minus-zero",
            "one",
            vec![("@DID@", "-0")],
            "sha256:f906c4d1e1ff54d6581b72066de2e1455818bbd33b387a2d0f6cf11116ac060d",
            Refused("missing_deployment_id"),
        ),
        // missing_node_id: mycelium_router/validation.py:24 from :91
        (
            "node-id-minus-zero",
            "one",
            vec![("@NODE@", "-0")],
            "sha256:8eda30f5f58feaf1af1b7c6883b0da1286ef26b45a0c7ae2e3ec20103d1e7e31",
            Refused("missing_node_id"),
        ),
        // duplicate_stage_id: mycelium_router/validation.py:24 from :57
        (
            "stage-id-minus-zero-then-zero",
            "two",
            vec![
                ("@S0@", "-0"),
                ("@S1@", "0"),
                ("@ENTRY@", "-0"),
                ("@FINAL@", "0"),
            ],
            "sha256:f53dac859929b859ebb50b4b5f53c4a7d3047947c96b754bc0b44c1ebacc7913",
            Refused("duplicate_stage_id"),
        ),
        (
            "edge-endpoints-minus-zero-name-zero",
            "two",
            vec![
                ("@A@", "0"),
                ("@B@", "1"),
                ("@E0FROM@", "-0"),
                ("@E0TO@", "1"),
                ("@L0FROM@", "1"),
                ("@L0TO@", "-0"),
            ],
            "sha256:bd51bcd60aad493d0dffecbbbafaab478eb6390f3b76588aa3e49d81e8c4f992",
            Graph(
                "sha256:c1c73d10322028eb4dc9a81304dd00ed61c97e600519a5301a3300fe919acda9",
                None,
            ),
        ),
        // duplicate_edge_pair: mycelium_router/validation.py:24 from :147
        (
            "edge-pair-minus-zero-repeats-zero",
            "two",
            vec![
                ("@A@", "0"),
                ("@B@", "1"),
                ("@E0FROM@", "0"),
                ("@E0TO@", "1"),
                ("@L0FROM@", "1"),
                ("@L0TO@", "0"),
                (
                    "@EXTRA@",
                    r#",{"edge_id":"edge-1","from_placement_id":-0,"to_placement_id":1,"link_id":"link-1"}"#,
                ),
            ],
            "sha256:b749ab96df52b452c74a3753fbd495a38e4f0fda9446a9654a4dd15b28e5d6e7",
            Refused("duplicate_edge_pair"),
        ),
        // duplicate_edge_id: mycelium_router/validation.py:24 from :159
        (
            "forward-id-minus-zero-loopback-id-zero",
            "two",
            vec![("@E0@", "-0"), ("@L0@", "0")],
            "sha256:1ec7bfc5fbd9307961dbe7dec768d5bcc155e1971c738db0eabe3f1f12f649fe",
            Refused("duplicate_edge_id"),
        ),
        // unknown_edge_placement: mycelium_router/validation.py:24 from :134
        (
            "edge-endpoint-minus-one-control",
            "two",
            vec![
                ("@A@", "0"),
                ("@B@", "1"),
                ("@E0FROM@", "-1"),
                ("@E0TO@", "1"),
                ("@L0FROM@", "1"),
                ("@L0TO@", "0"),
            ],
            "sha256:e2db71600ef0842f5ff13951d08ce58afb69babb0f6804a61b78ec907331c90b",
            Refused("unknown_edge_placement"),
        ),
    ];
    let fill = |template: &str, subs: &[(&str, &str)]| {
        let (mut text, defaults) = match template {
            "one" => (ONE_STAGE.to_owned(), &one_defaults[..]),
            _ => (TWO_STAGE.to_owned(), &two_defaults[..]),
        };
        assert!(
            subs.iter()
                .all(|(k, _)| defaults.iter().any(|(d, _)| d == k))
        );
        for (key, default) in defaults {
            let value = subs
                .iter()
                .find(|(k, _)| k == key)
                .map_or(*default, |(_, v)| *v);
            text = text.replace(key, value);
        }
        assert!(!text.contains('@'), "{text}");
        text
    };
    let parsed = |text: &str| {
        let document: Value = serde_json::from_str(text).expect("raw JSON text");
        execution_graph::parse(&document)
    };
    let canonical_sha256 = |graph: &execution_graph::Graph| {
        canonical::sha256_prefixed(&canonical::to_bytes(graph.as_value()).unwrap())
    };
    let mut mismatches = Vec::new();
    for (name, template, subs, text_sha256, expect) in &cases {
        let text = fill(template, subs);
        assert_eq!(
            canonical::sha256_prefixed(text.as_bytes()),
            *text_sha256,
            "{name}: the text the oracle read"
        );
        let (matched, actual) = match (expect, parsed(&text)) {
            (Graph(sha, bits), Ok(graph)) => {
                let actual_sha = canonical_sha256(&graph);
                let actual_bits = bits.and_then(|(at, _)| {
                    graph
                        .as_value()
                        .pointer(at)
                        .and_then(Value::as_f64)
                        .map(f64::to_bits)
                });
                (
                    actual_sha == *sha && actual_bits == bits.map(|(_, b)| b),
                    format!("graph {actual_sha}, bits {actual_bits:x?}"),
                )
            }
            (_, Ok(graph)) => (false, format!("graph {}", canonical_sha256(&graph))),
            (Refused(code), Err(error)) => (error.0 == *code, format!("refused {}", error.0)),
            (Raised(exception), Err(error)) => (
                *exception != "ContractError" && !error.0.is_empty(),
                format!("refused {}", error.0),
            ),
            (Graph(..), Err(error)) => (false, format!("refused {}", error.0)),
        };
        if !matched {
            mismatches.push(format!("{name}: oracle {expect:?}, parser {actual}"));
        }
    }
    assert_eq!(mismatches, Vec::<String>::new(), "raw-lexeme graph parity");
    // A `-0` KV cost is the `0` graph: the same canonical bytes.
    let bytes = |subs: &[(&str, &str)]| {
        parsed(&fill("one", subs)).map(|graph| canonical::to_bytes(graph.as_value()).unwrap())
    };
    assert_eq!(bytes(&[("@KV@", "-0")]), bytes(&[]));
}
