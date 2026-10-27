# SPDX-License-Identifier: AGPL-3.0-only
"""Normal unsigned Mac launcher: no source edits or network IDs required."""
import sys

#: Byte-identical copy of ``mycelium_node.process._LAUNCHER_SOURCE`` (pinned by
#: tests/a13_macos/test_launcher_entry_is_cheap.py).  A frozen app runs
#: ``<exe> --node-process-launcher ...`` for every node process, and its ready
#: byte is inside the 2.0 s launch budget, so this entry must reach the
#: trampoline before importing the app (or even the node package): only ``sys``
#: is imported above this line.
_NODE_PROCESS_LAUNCHER_SOURCE = """import os
import sys

ready = int(sys.argv[1])
release = int(sys.argv[2])
exec_status = int(sys.argv[3])
target = sys.argv[4:]
try:
    os.set_inheritable(exec_status, False)
    os.write(ready, b"R")
    os.close(ready)
    if os.read(release, 1) != b"G":
        os._exit(125)
    os.close(release)
    os.execv(target[0], target)
except BaseException:
    try:
        os.write(exec_status, b"E")
    except BaseException:
        pass
    os._exit(126)
"""


def _run_node_process_launcher(argv):
    """Fixed packaged trampoline, never caller-supplied Python source."""
    sys.argv=list(argv[1:])
    exec(compile(_NODE_PROCESS_LAUNCHER_SOURCE, '<node-process-launcher>', 'exec'), {'__name__':'__main__'})
    return 126


if __name__=='__main__' and sys.argv[1:2]==['--node-process-launcher']:
    raise SystemExit(_run_node_process_launcher(sys.argv))

import argparse
import json
from pathlib import Path
import signal
import webbrowser
from mycelium_a13_macos.app import DemoApp, DemoError, bounded_failure_locus, preflight, reopen_native, model_contract, public_failure_code


def default_state_root(*, platform=None, environ=None):
    """macOS keeps its historical folder; Windows uses the per-user installer's state root."""
    import os
    platform=sys.platform if platform is None else platform
    environ=os.environ if environ is None else environ
    if platform=='win32':
        local=environ.get('LOCALAPPDATA')
        base=Path(local) if local else Path.home()/'AppData'/'Local'
        return base/'Mycelium'/'A13'
    if platform.startswith('linux'):
        # XDG Base Directory: state belongs in XDG_STATE_HOME (a relative value is ignored).
        state=environ.get('XDG_STATE_HOME')
        base=Path(state) if state and os.path.isabs(state) else Path.home()/'.local'/'state'
        return base/'mycelium'/'A13'
    return Path.home()/'Library'/'Application Support'/'Mycelium A13 macOS Demo'


_INSTALLER_ACTIONS=('--install','--uninstall','--rollback','--install-status')

#: ``python -m`` entry points a *frozen* app answers for, so the shared service
#: renderer's ``<exe> -B -m mycelium_service_runner ...`` and the host's
#: ``<exe> -m <host> ...`` work with the frozen executable as argv[0].  A closed
#: allow-list: never an arbitrary module, never caller-supplied source.
_FROZEN_MODULES={
    'mycelium_service_runner':'mycelium_service_runner',
    'mycelium_a13_windows.managed_host':'mycelium_a13_windows.managed_host.__main__',
    'mycelium_a13_linux.managed_host':'mycelium_a13_linux.managed_host.__main__',
}


def _frozen_module_main(argv):
    rest=argv[1:]
    if rest[:1]==['-B']: rest=rest[1:]
    if len(rest)<2 or rest[0]!='-m' or rest[1] not in _FROZEN_MODULES: return None
    import importlib
    module=importlib.import_module(_FROZEN_MODULES[rest[1]])
    sys.argv=[rest[1],*rest[2:]]
    return module.main(rest[2:])


def _show_refusal_dialog(message):
    """A Finder launch has no terminal: show the refusal as a native alert."""
    import subprocess
    script=('display alert "Mycelium A13 Demo cannot run on this Mac" message '
            + json.dumps(message) + ' as critical buttons {"OK"} default button "OK"')
    try:subprocess.run(['/usr/bin/osascript','-e',script],timeout=300,check=False,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    except Exception:pass


def _refuse_uninstalled_launch():
    """A bare double-click of the packaged app outside programs/<version> is not an install: say so, start nothing."""
    from mycelium_a13_macos import launch_location
    refusal=launch_location.not_installed_refusal(frozen=bool(getattr(sys,'frozen',False)),platform=sys.platform,
        argv=sys.argv,executable=Path(sys.executable),programs_root=launch_location.programs_root(Path.home()),
        applications_entry=Path.home()/'Applications'/'Mycelium A13.app')
    if refusal is None: return None
    print(json.dumps(refusal),file=sys.stderr)
    _show_refusal_dialog(refusal['message'])
    return 2


def main():
    if not sys.argv[1:] or sys.argv[1].startswith('-psn_'):
        refused=_refuse_uninstalled_launch()
        if refused is not None: return refused
    if sys.argv[1:2] in (['-B'],['-m']):
        result=_frozen_module_main(sys.argv)
        if result is not None: return result
        print(json.dumps({'event':'launcher_refused','code':'frozen_module_not_allowed'}),file=sys.stderr)
        return 2
    if sys.argv[1:2] and sys.argv[1] in _INSTALLER_ACTIONS:
        # Per-user installer of the frozen app for this platform.
        if sys.platform=='win32':
            from mycelium_a13_windows.installer import main as installer_main
        elif sys.platform.startswith('linux'):
            from mycelium_a13_linux.installer import main as installer_main
        elif sys.platform=='darwin':
            from mycelium_a13_macos.installer import main as installer_main
        else:
            print(json.dumps({'event':'launcher_refused','code':'installer_unsupported_platform'}),file=sys.stderr)
            return 2
        return installer_main(sys.argv[1:])
    if sys.argv[1:2]==['--node-process-launcher']:
        # Normally dispatched at import time above; kept for in-process callers.
        return _run_node_process_launcher(sys.argv)
    if sys.argv[1:2]==['--compile-route']:
        del sys.argv[1]
        from scripts.build_qwen_live_route import main as compile_main
        return compile_main()
    if sys.argv[1:2]==['--managed-stage']:
        del sys.argv[1]
        from mycelium_a13_macos.managed_staging import canonical_main
        return canonical_main()
    if sys.argv[1:2]==['--managed-node']:
        del sys.argv[1]
        from mycelium_a13_macos.worker import native_main
        return native_main()
    parser=argparse.ArgumentParser(description='macOS A13 hackathon demo — unsigned development software')
    parser.add_argument('--state-root',type=Path,default=default_state_root())
    parser.add_argument('--headless',action='store_true',help='Do not open a browser (local integration tests)')
    probes=parser.add_mutually_exclusive_group()
    probes.add_argument('--preflight',action='store_true',help='Check platform without creating identity or state')
    probes.add_argument('--runtime-probe',action='store_true',help='Explicit tiny GPU arithmetic probe; no model or identity')
    from mycelium_platform_neutral.offline import add_offline_argument, apply_offline
    add_offline_argument(parser)
    args=parser.parse_args()
    # --offline / MYCELIUM_OFFLINE / MYCELIUM_NETWORK_MODE=lan|local: no Hugging Face contact.
    apply_offline(args.offline,None)
    app=None
    from contextlib import ExitStack
    process_settings=ExitStack()
    try:
        report=preflight()
        if args.preflight:
            print(json.dumps(report)); return 0
        if args.runtime_probe:
            from mycelium_a13_macos.capability import runtime_probe_report
            value=runtime_probe_report();print(json.dumps(value))
            return 0 if value['available'] else 2
        try:
            app=DemoApp(args.state_root).start()
            from mycelium_a13_macos.fast_mode import bind_owner_process
            process_settings.enter_context(bind_owner_process(app))
        except DemoError as error:
            if str(error) != 'already_running': raise
            launch=reopen_native(args.state_root)
            if not args.headless: webbrowser.open(launch.as_uri())
            print(json.dumps({'event':'private_browser_reopened','model_contract':model_contract()})); return 0
        # Set from the signal handler while this (main) thread waits on it: a
        # threading.Event would take the lock its own wait() holds.
        from mycelium_host_platform import StopRequest
        done=StopRequest()
        def stop(signum,frame): done.set()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        if not args.headless: webbrowser.open(app.launch_file.as_uri())
        print(json.dumps({'event':'launcher_started','distribution':'unsigned_development_demo','serving_role':'not_yet_serving','model_contract':model_contract()}),flush=True)
        while not done.wait(.25) and not app.closed.is_set(): pass
        return 0
    except DemoError as error:
        message=getattr(error,'user_message',None)
        print(json.dumps({'event':'launcher_refused','code':str(error),
            **({'observed_platform':error.observed_platform} if hasattr(error,'observed_platform') else {}),
            **({'message':message} if message else {})}),file=sys.stderr)
        if message and not args.headless and not args.preflight and sys.platform=='darwin':
            _show_refusal_dialog(message)
        return 2
    except Exception as error:
        # Never expose exception strings that might carry request/state material.
        # The bounded locus (root-relative, allowlisted) names WHICH state file
        # failed without leaking an absolute path or any exception text.
        locus=bounded_failure_locus(error,args.state_root)
        print(json.dumps({'event':'launcher_failed','code':public_failure_code(error),'error_type':type(error).__name__,**locus,'state_retained':True}),file=sys.stderr)
        return 3
    finally:
        try:
            if app is not None: app.close()
        finally:
            process_settings.close()

if __name__=='__main__': raise SystemExit(main())
