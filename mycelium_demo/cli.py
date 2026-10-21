# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from .device_lab import DeviceLabError, prepare_device_lab
from mycelium_platform_neutral import network_mode as network_mode_module
from mycelium_platform_neutral.lan_doctor import (
    DEFAULT_PORTS as LAN_DEFAULT_PORTS,
    render_report as render_lan_report,
    run_lan_doctor,
)
from mycelium_platform_neutral.offline import (
    OfflineSettingError,
    add_offline_argument,
    apply_offline,
    offline_message,
)

from .doctor import (
    DEFAULT_COMMANDS,
    canonical_json,
    local_tcp_port_available,
    run_preflight,
)

ROOT = Path(__file__).resolve().parents[1]


def _port_value(value: str) -> int | str:
    if value.isascii() and value.isdecimal():
        significant = value.lstrip("0") or "0"
        if len(significant) <= 5:
            return int(significant, 10)
    return value


def _serve_port(value: str) -> int:
    parsed = _port_value(value)
    if isinstance(parsed, int) and 1 <= parsed <= 65_535:
        return parsed
    raise argparse.ArgumentTypeError("port must be an integer from 1 through 65535")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3.14 -m mycelium_demo",
        description="Mycelium local preflight and explicitly bounded demo launchers.",
    )
    commands = parser.add_subparsers(dest="action", required=True)

    doctor = commands.add_parser(
        "doctor", help="run local non-mutating prerequisite checks"
    )
    doctor.add_argument("--repo-root", type=Path, default=ROOT)
    doctor.add_argument("--state-dir", type=Path)
    doctor.add_argument(
        "--lan",
        action="store_true",
        help="check this device for a swarm on your own network, with no hosted service",
    )
    doctor.add_argument("--json", action="store_true", help="with --lan: print JSON")
    doctor.add_argument(
        "--origin",
        action="append",
        default=[],
        help="with --lan: an origin you plan to use, checked for hosted services",
    )
    network_mode_module.add_network_mode_argument(doctor)
    add_offline_argument(doctor)
    doctor.add_argument(
        "--port",
        type=_port_value,
        action="append",
        default=[],
        help="intended local TCP listener port to probe; repeat for multiple ports",
    )

    models = commands.add_parser("models", help="manage the local model cache")
    model_commands = models.add_subparsers(dest="models_action", required=True)
    importer = model_commands.add_parser(
        "import",
        help="verify a model folder against the pinned manifest and install it read-only",
    )
    importer.add_argument("folder", type=Path, help="folder holding the model files")
    importer.add_argument("--model", required=True, help="pinned model id, e.g. Qwen/Qwen2.5-7B")
    importer.add_argument("--commit", required=True, help="pinned 40-character model commit")
    importer.add_argument(
        "--cache-root",
        required=True,
        type=Path,
        help="model cache folder (the 'model-source' folder inside the app's state folder)",
    )
    importer.add_argument(
        "--manifest",
        type=Path,
        help="the pinned manifest file (file list, sizes, digests); required when offline",
    )
    network_mode_module.add_network_mode_argument(importer)
    add_offline_argument(importer)

    fetcher = model_commands.add_parser(
        "fetch-manifest",
        help="save the pinned manifest (online, once) for later offline imports",
    )
    fetcher.add_argument("--model", required=True, help="pinned model id")
    fetcher.add_argument("--commit", required=True, help="pinned 40-character model commit")
    fetcher.add_argument("--out", required=True, type=Path, help="new manifest file to write")
    network_mode_module.add_network_mode_argument(fetcher)
    add_offline_argument(fetcher)

    device_lab = commands.add_parser(
        "device-lab",
        help="prepare trusted LAN HTTPS and launch a bounded physical-browser swarm",
    )
    device_lab.add_argument(
        "--advertise-host",
        required=True,
        help="non-loopback LAN IP address or DNS name reachable by every test device",
    )
    device_lab.add_argument("--bind-host", choices=("0.0.0.0",), default="0.0.0.0")
    device_lab.add_argument("--port", type=_serve_port, default=8787)
    device_lab.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".mycelium" / "device-lab",
    )
    device_lab.add_argument(
        "--static-root",
        type=Path,
        help="product operator SPA root; use with --worker-static-root for split rich UI",
    )
    device_lab.add_argument(
        "--worker-static-root",
        type=Path,
        help="browser-worker static root mounted at /device",
    )

    demo_stack = commands.add_parser(
        "demo-stack",
        help=(
            "launch the loopback demo product stack together with the "
            "OpenAI-compatible shim co-listener and the real Observatory gateway"
        ),
        description=(
            "Compose mycelium_demo.product_stack with the real "
            "mycelium_gateway.asgi observatory application mounted, co-launch the "
            "mycelium_openai_shim HTTP listener beside it, and serve both on "
            "loopback. Stub/synthetic qualification only: this is reachability "
            "and composition evidence, never physical route evidence."
        ),
    )
    demo_stack.add_argument(
        "--host",
        choices=("127.0.0.1",),
        default="127.0.0.1",
        help="loopback host for both listeners",
    )
    demo_stack.add_argument(
        "--port",
        type=_serve_port,
        default=0,
        help="product stack loopback port (0 = ephemeral)",
    )
    demo_stack.add_argument(
        "--openai-shim-port",
        type=_serve_port,
        default=0,
        help="OpenAI-compatible shim loopback port (0 = ephemeral)",
    )
    demo_stack.add_argument(
        "--state-root",
        type=Path,
        default=Path.home() / ".mycelium" / "demo-stack",
        help="private state root for the observatory publisher",
    )
    demo_stack.add_argument(
        "--request-bearer-token-env",
        default="MYCELIUM_DEMO_REQUEST_BEARER_TOKEN",
        metavar="NAME",
        help=(
            "name of the environment variable holding the product gateway "
            "credential; the credential value itself is never accepted as an "
            "argument and is never printed"
        ),
    )
    demo_stack.add_argument(
        "--status-only",
        action="store_true",
        help="bring both listeners up, print the status document, and shut down",
    )

    serve = commands.add_parser(
        "serve",
        help="launch bundled fixture UI or a qualified physical inference route",
    )
    serve.add_argument(
        "--mode",
        choices=("fixture", "live"),
        required=True,
        help=(
            "fixture serves bundled synthetic product data; live serves only a "
            "qualified physical route"
        ),
    )
    network_mode_module.add_network_mode_argument(serve)
    add_offline_argument(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=_serve_port)
    serve.add_argument("--state-root", type=Path)
    serve.add_argument("--operator-token-file", type=Path)
    serve.add_argument("--public-origin")
    serve.add_argument(
        "--trusted-https-proxy",
        action="store_true",
        help="trust an authenticated HTTPS loopback proxy capability",
    )
    serve.add_argument("--trusted-proxy-capability-file", type=Path)
    serve.add_argument("--static-root", type=Path)
    serve.add_argument("--worker-static-root", type=Path)
    serve.add_argument("--tls-cert", type=Path)
    serve.add_argument("--tls-key", type=Path)
    serve.add_argument("--operator-plan", type=Path, action="append")
    serve.add_argument("--deployment-dir", type=Path)
    serve.add_argument("--model-operation-file", type=Path)
    serve.add_argument("--model-cache-root", type=Path)
    serve.add_argument("--model-capacity-live-observations-file", type=Path)
    serve.add_argument("--registry-state", type=Path)
    serve.add_argument("--historical-evidence-file", type=Path, action="append")
    serve.add_argument("--a4-positive-observation", type=Path, action="append")
    serve.add_argument("--a4-negative-data-plane-observation", type=Path, action="append")
    serve.add_argument("--a4-negative-qualification-observation", type=Path)
    serve.add_argument("--a4-negative-shutdown-observation", type=Path)
    serve.add_argument("--replica-qualification", type=Path, action="append")
    serve.add_argument("--save-live-qualification", type=Path)
    serve.add_argument("--seed-state-root", type=Path)
    serve.add_argument("--product-state-root", type=Path)
    serve.add_argument("--candidate-plan-root", type=Path)
    serve.add_argument("--artifact-acquisition-root", type=Path)
    serve.add_argument("--model-preparation-template-plan", type=Path)
    serve.add_argument("--model-preparation-execution-topology-plan", type=Path)
    serve.add_argument("--model-preparation-root", type=Path)
    serve.add_argument("--model-preparation-temporary-root", type=Path)
    serve.add_argument(
        "--model-preparation-artifact-bytes-per-second",
        type=int,
    )
    serve.add_argument("--member-artifact-transport-plan", type=Path)
    serve.add_argument("--member-model-inventory-file", type=Path, action="append")
    serve.add_argument("--member-model-inventory-authorities-file", type=Path)
    serve.add_argument("--representation-authorization-file", type=Path)
    serve.add_argument("--seed-url")
    serve.add_argument(
        "--a8-force-relay",
        action="store_true",
        help="operator-only physical qualification control; never authors path evidence",
    )
    serve.add_argument("--coarse-region-declaration-file", type=Path)
    serve.add_argument(
        "--persist-transport-history",
        action="store_true",
        help="operator-only A14 control: keep the direct/relay ledger across planned restarts",
    )
    serve.add_argument(
        "--a6-fault-control-port",
        type=int,
        help="grant-gated A6 controlled-fault loopback port for a live physical gate",
    )
    serve.add_argument(
        "--a10-verify-control-port",
        type=int,
        help="grant-gated A10 target-verification loopback port for a live physical gate",
    )
    serve.add_argument(
        "--a10-batch-policy-control-port",
        type=int,
        help="grant-gated A10 batch-policy loopback port for the paired benchmark",
    )
    serve.add_argument(
        "--a7-evidence-control-port",
        type=int,
        help="grant-gated, read-only A7 evidence loopback port for the A7 physical gate",
    )
    serve.add_argument(
        "--a11-evidence-control-port",
        type=int,
        help="grant-gated, read-only A11 node evidence loopback port for the A11 benchmark",
    )
    serve.add_argument(
        "--a11-draft-loss-control-port",
        type=int,
        help="grant-gated, request-scoped A11 draft-loss loopback port for the A11 physical gate",
    )
    serve.add_argument(
        "--a9-authority-fault-control-port",
        type=int,
        help="grant-gated A9 authority fault publisher loopback port for the A9 physical gate",
    )
    return parser


def _live_arguments(args: argparse.Namespace) -> list[str]:
    result = [
        "--host",
        args.host,
        "--port",
        str(args.port if args.port is not None else 8787),
    ]
    for operator_plan in args.operator_plan or ():
        result.extend(("--operator-plan", str(operator_plan)))
    for evidence_file in args.historical_evidence_file or ():
        result.extend(("--historical-evidence-file", str(evidence_file)))
    for observation in args.a4_positive_observation or ():
        result.extend(("--a4-positive-observation", str(observation)))
    for observation in args.a4_negative_data_plane_observation or ():
        result.extend(("--a4-negative-data-plane-observation", str(observation)))
    if args.a4_negative_qualification_observation is not None:
        result.extend(
            (
                "--a4-negative-qualification-observation",
                str(args.a4_negative_qualification_observation),
            )
        )
    if args.a4_negative_shutdown_observation is not None:
        result.extend(
            (
                "--a4-negative-shutdown-observation",
                str(args.a4_negative_shutdown_observation),
            )
        )
    for qualification in args.replica_qualification or ():
        result.extend(("--replica-qualification", str(qualification)))
    if args.save_live_qualification is not None:
        result.extend(("--save-live-qualification", str(args.save_live_qualification)))
    for inventory_file in args.member_model_inventory_file or ():
        result.extend(("--member-model-inventory-file", str(inventory_file)))
    optional_paths = (
        ("--deployment-dir", args.deployment_dir),
        ("--model-operation-file", args.model_operation_file),
        ("--model-cache-root", args.model_cache_root),
        (
            "--model-capacity-live-observations-file",
            args.model_capacity_live_observations_file,
        ),
        ("--static-root", args.static_root),
        ("--registry-state", args.registry_state),
        ("--seed-state-root", args.seed_state_root),
        ("--product-state-root", args.product_state_root),
        ("--candidate-plan-root", args.candidate_plan_root),
        ("--artifact-acquisition-root", args.artifact_acquisition_root),
        ("--model-preparation-template-plan", args.model_preparation_template_plan),
        (
            "--model-preparation-execution-topology-plan",
            args.model_preparation_execution_topology_plan,
        ),
        ("--model-preparation-root", args.model_preparation_root),
        (
            "--model-preparation-temporary-root",
            args.model_preparation_temporary_root,
        ),
        ("--member-artifact-transport-plan", args.member_artifact_transport_plan),
        (
            "--member-model-inventory-authorities-file",
            args.member_model_inventory_authorities_file,
        ),
        (
            "--representation-authorization-file",
            args.representation_authorization_file,
        ),
    )
    for flag, value in optional_paths:
        if value is not None:
            result.extend((flag, str(value)))
    if args.model_preparation_artifact_bytes_per_second is not None:
        result.extend(
            (
                "--model-preparation-artifact-bytes-per-second",
                str(args.model_preparation_artifact_bytes_per_second),
            )
        )
    if args.seed_url is not None:
        result.extend(("--seed-url", args.seed_url))
    if args.public_origin is not None:
        result.extend(("--public-origin", args.public_origin))
    if args.trusted_https_proxy:
        result.append("--trusted-https-proxy")
    if args.trusted_proxy_capability_file is not None:
        result.extend(
            (
                "--trusted-proxy-capability-file",
                str(args.trusted_proxy_capability_file),
            )
        )
    if args.a8_force_relay:
        result.append("--a8-force-relay")
    if args.a6_fault_control_port is not None:
        result.extend(("--a6-fault-control-port", str(args.a6_fault_control_port)))
    if args.a10_verify_control_port is not None:
        result.extend(("--a10-verify-control-port", str(args.a10_verify_control_port)))
    if args.a10_batch_policy_control_port is not None:
        result.extend(
            ("--a10-batch-policy-control-port", str(args.a10_batch_policy_control_port))
        )
    if args.coarse_region_declaration_file is not None:
        result.extend(
            ("--coarse-region-declaration-file", str(args.coarse_region_declaration_file))
        )
    if args.persist_transport_history:
        result.append("--persist-transport-history")
    if args.a7_evidence_control_port is not None:
        result.extend(("--a7-evidence-control-port", str(args.a7_evidence_control_port)))
    if args.a11_evidence_control_port is not None:
        result.extend(("--a11-evidence-control-port", str(args.a11_evidence_control_port)))
    if args.a11_draft_loss_control_port is not None:
        result.extend(("--a11-draft-loss-control-port", str(args.a11_draft_loss_control_port)))
    if args.a9_authority_fault_control_port is not None:
        result.extend(("--a9-authority-fault-control-port", str(args.a9_authority_fault_control_port)))
    return result


def _fixture_has_live_only_arguments(args: argparse.Namespace) -> bool:
    live_only_switches = (
        args.a8_force_relay or args.trusted_https_proxy or args.persist_transport_history
    )
    return live_only_switches or any(
        value is not None
        for value in (
            args.state_root,
            args.operator_token_file,
            args.public_origin,
            args.trusted_proxy_capability_file,
            args.static_root,
            args.worker_static_root,
            args.tls_cert,
            args.tls_key,
            args.operator_plan,
            args.seed_state_root,
            args.product_state_root,
            args.deployment_dir,
            args.model_operation_file,
            args.model_cache_root,
            args.model_capacity_live_observations_file,
            args.registry_state,
            args.historical_evidence_file,
            args.candidate_plan_root,
            args.artifact_acquisition_root,
            args.model_preparation_template_plan,
            args.model_preparation_execution_topology_plan,
            args.model_preparation_root,
            args.model_preparation_temporary_root,
            args.model_preparation_artifact_bytes_per_second,
            args.member_artifact_transport_plan,
            args.member_model_inventory_file,
            args.member_model_inventory_authorities_file,
            args.representation_authorization_file,
            args.seed_url,
            args.a6_fault_control_port,
            args.coarse_region_declaration_file,
            args.a10_verify_control_port,
            args.a10_batch_policy_control_port,
            args.a7_evidence_control_port,
            args.a11_evidence_control_port,
            args.a11_draft_loss_control_port,
            args.a9_authority_fault_control_port,
        )
    )


def _resolve_pinned_manifest(model, resolver):
    if resolver is None:
        from mycelium_a13_macos.acquisition import _HTTPRepositoryTransport

        resolver = _HTTPRepositoryTransport().resolve
    return resolver(model)


def _fetch_manifest(args, model, offline, resolver, fail) -> int:
    """``models fetch-manifest``: save the pinned manifest once, while online."""
    from mycelium_a13_macos.acquisition import AcquisitionError, _validate_manifest

    if offline:
        return fail(offline_message("the pinned file list"))
    try:
        document = _resolve_pinned_manifest(model, resolver)
        if (
            not isinstance(document, Mapping)
            or document.get("model_id") != model.model_id
            or document.get("resolved_commit") != model.resolved_commit
        ):
            return fail(
                f"the manifest received is not for {model.model_id} at the pinned commit "
                f"{model.resolved_commit[:12]}, so it was not saved."
            )
        manifest = _validate_manifest(document, model=model)
    except AcquisitionError as exc:
        return fail(f"the pinned file list could not be used ({exc.code}).")
    except Exception as exc:
        return fail(
            f"the pinned file list could not be fetched ({getattr(exc, 'code', type(exc).__name__)})."
        )
    try:
        descriptor = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return fail(f"{args.out} already exists; choose a new file name.")
    except OSError:
        return fail(f"{args.out} could not be created.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(canonical_json(manifest) + "\n")
    print(canonical_json({"manifest": str(args.out), "model_id": model.model_id,
                          "resolved_commit": model.resolved_commit,
                          "files": len(manifest["files"])}))
    return 0


def _models_import(args, environ, resolver) -> int:
    """``models import``: plain-English failures, exit status 1, nothing partial installed."""
    import json
    import sys

    from mycelium_a13_macos.model_import import ModelImportError, import_model_folder
    from mycelium_a13_macos.serving import ServingPreparationError, registered_model_contract

    def fail(message: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        return 1

    env = os.environ if environ is None else dict(environ)
    try:
        offline = apply_offline(args.offline, args.network_mode, env)
    except (OfflineSettingError, ValueError) as exc:
        return fail(str(exc))
    try:
        model = registered_model_contract(args.model, args.commit)
    except ServingPreparationError:
        return fail(
            f"{args.model} at {args.commit[:12]} is not a supported pinned model in this "
            "version, so it cannot be imported."
        )
    if args.models_action == "fetch-manifest":
        return _fetch_manifest(args, model, offline, resolver, fail)
    if args.manifest is not None:
        try:
            raw = args.manifest.read_bytes()
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("too large")
            manifest = json.loads(raw)
        except (OSError, ValueError):
            return fail(f"the manifest file {args.manifest} could not be read as JSON.")
    elif offline:
        return fail(
            "no pinned manifest was given. "
            + offline_message("the pinned file list")
            + " Pass --manifest FILE with the pinned manifest the owner gave you."
        )
    else:
        try:
            manifest = _resolve_pinned_manifest(model, resolver)
        except Exception as exc:
            return fail(
                "the pinned file list could not be fetched "
                f"({getattr(exc, 'code', type(exc).__name__)}). Pass --manifest FILE instead."
            )
    try:
        result = import_model_folder(
            args.folder, manifest=manifest, model=model, cache_root=args.cache_root
        )
    except ModelImportError as exc:
        return fail(str(exc))
    except Exception as exc:
        return fail(
            "the model cache could not be written "
            f"({getattr(exc, 'code', type(exc).__name__)}). Check the --cache-root folder."
        )
    print(canonical_json(result))
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    which=shutil.which,
    port_available=local_tcp_port_available,
    process_runner=subprocess.run,
    live_server_main=None,
    environ: Mapping[str, str] | None = None,
    fixture_runtime_available=lambda path: path.is_file(),
    lan_address=network_mode_module.route_lookup_ipv4,
    device_lab_prepare=prepare_device_lab,
    demo_stack_bringup=None,
    demo_stack_inputs_factory=None,
    model_manifest_resolver=None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.action == "doctor" and args.lan:
        lan_report = run_lan_doctor(
            environ=os.environ if environ is None else environ,
            network_mode=args.network_mode,
            offline=args.offline,
            flags=tuple(
                token for origin in args.origin for token in ("--origin", origin)
            ),
            ports=tuple(args.port) or LAN_DEFAULT_PORTS,
            lan_address=lan_address,
            port_available=port_available,
        )
        print(canonical_json(lan_report) if args.json else render_lan_report(lan_report), end="")
        if args.json:
            print()
        return 0 if lan_report["ok"] else 1

    if args.action == "doctor":
        if args.state_dir is None:
            parser.error("the following arguments are required: --state-dir")
        report = run_preflight(
            repo_root=args.repo_root,
            state_dir=args.state_dir,
            commands=DEFAULT_COMMANDS,
            ports=args.port,
            which=which,
            port_available=port_available,
        )
        print(canonical_json(report))
        return 0 if report["local_preflight_ok"] else 1

    if args.action == "device-lab":
        openssl = which("openssl")
        if openssl is None:
            parser.error("device-lab requires OpenSSL")
        try:
            bundle = device_lab_prepare(
                state_root=args.state_root,
                advertise_host=args.advertise_host,
                port=args.port,
                openssl=openssl,
            )
        except DeviceLabError as exc:
            parser.error(str(exc))
        print(
            canonical_json(
                {
                    "protocol": "mycelium.device_lab_prepared.v1",
                    "advertise_host": bundle.advertise_host,
                    "port": bundle.port,
                    "public_origin": bundle.public_origin,
                    "ca_cert": str(bundle.ca_cert),
                    "device_setup": [
                        "transfer_and_trust_ca",
                        "open_unique_join_link_per_device",
                        "wait_until_each_device_is_ready",
                        "run_bounded_test_inference",
                    ],
                    "route_ready": False,
                    "local_evidence_only": True,
                    "tailscale_required": False,
                }
            ),
            flush=True,
        )
        if live_server_main is None:
            from mycelium_interactive.server import main as live_server_main

        live_arguments = [
            "--host",
            args.bind_host,
            "--port",
            str(bundle.port),
            "--state-root",
            str(bundle.runtime_root),
        ]
        for flag, value in (
            ("--static-root", args.static_root),
            ("--worker-static-root", args.worker_static_root),
        ):
            if value is not None:
                live_arguments.extend((flag, str(value)))
        live_arguments.extend(
            [
                "--public-origin",
                bundle.public_origin,
                "--tls-cert",
                str(bundle.tls_cert),
                "--tls-key",
                str(bundle.tls_key),
            ]
        )
        return int(live_server_main(live_arguments))

    if args.action == "models":
        return _models_import(args, environ, model_manifest_resolver)

    if args.action == "demo-stack":
        if demo_stack_inputs_factory is None:
            from .product_servers import build_stub_demo_stack_inputs as demo_stack_inputs_factory
        if demo_stack_bringup is None:
            from .product_servers import bring_up_demo_product_servers as demo_stack_bringup

        stack_environ = os.environ if environ is None else environ
        inputs = dict(demo_stack_inputs_factory())
        bearer_override = stack_environ.get(args.request_bearer_token_env)
        if bearer_override:
            inputs["request_bearer_token"] = bearer_override
        servers = demo_stack_bringup(
            **inputs,
            state_root=args.state_root,
            product_host=args.host,
            product_port=args.port,
            openai_shim_host=args.host,
            openai_shim_port=args.openai_shim_port,
        )
        with servers:
            print(canonical_json(dict(servers.status())), flush=True)
            if not args.status_only:
                try:
                    while True:
                        time.sleep(0.5)
                except KeyboardInterrupt:
                    pass
        return 0

    if args.action != "serve":
        raise AssertionError(f"unsupported action: {args.action}")
    # lan/local imply --offline unless overridden; internet leaves the environment alone.
    try:
        apply_offline(
            args.offline, args.network_mode, os.environ if environ is None else environ
        )
    except (OfflineSettingError, ValueError) as exc:
        parser.error(str(exc))
    if args.mode == "live":
        if args.operator_plan is None:
            parser.error("live mode requires --operator-plan")
        if args.seed_state_root is None:
            parser.error("live mode requires --seed-state-root")
        if not (
            args.trusted_https_proxy
            == (args.public_origin is not None)
            == (args.trusted_proxy_capability_file is not None)
        ):
            parser.error(
                "--trusted-https-proxy, --public-origin, and "
                "--trusted-proxy-capability-file require each other"
            )
        if any(
            value is not None
            for value in (
                args.state_root,
                args.operator_token_file,
                args.worker_static_root,
                args.tls_cert,
                args.tls_key,
            )
        ):
            parser.error(
                "physical live mode is loopback-only and rejects interactive-runtime options"
            )
        if live_server_main is None:
            from mycelium_live.supervisor import main as live_server_main

        return int(live_server_main(_live_arguments(args)))
    if _fixture_has_live_only_arguments(args):
        parser.error("fixture mode does not accept live-runtime arguments")

    fixture_environment = dict(os.environ if environ is None else environ)
    fixture_environment["VITE_OBSERVATORY_SOURCE_MODE"] = "fixture"
    fixture_root = ROOT / "ui" / "web"
    fixture_vite = fixture_root / "node_modules" / ".bin" / "vite"
    if not fixture_runtime_available(fixture_vite):
        bootstrap = process_runner(
            ["npm", "ci"],
            cwd=fixture_root,
            env=fixture_environment,
            check=False,
        )
        bootstrap_exit = int(bootstrap.returncode)
        if bootstrap_exit != 0:
            return bootstrap_exit
    result = process_runner(
        [
            "npm",
            "run",
            "dev",
            "--",
            "--host",
            args.host,
            "--port",
            str(args.port if args.port is not None else 5173),
            "--strictPort",
        ],
        cwd=fixture_root,
        env=fixture_environment,
        check=False,
    )
    return int(result.returncode)
