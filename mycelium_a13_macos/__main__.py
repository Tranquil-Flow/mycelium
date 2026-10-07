# SPDX-License-Identifier: AGPL-3.0-or-later
"""Normal unsigned Mac launcher: no source edits or network IDs required."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
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
}


def _frozen_module_main(argv):
    rest=argv[1:]
    if rest[:1]==['-B']: rest=rest[1:]
    if len(rest)<2 or rest[0]!='-m' or rest[1] not in _FROZEN_MODULES: return None
    import importlib
    module=importlib.import_module(_FROZEN_MODULES[rest[1]])
    sys.argv=[rest[1],*rest[2:]]
    return module.main(rest[2:])


def main():
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
        else:
            print(json.dumps({'event':'launcher_refused','code':'installer_unsupported_platform'}),file=sys.stderr)
            return 2
        return installer_main(sys.argv[1:])
    if sys.argv[1:2]==['--node-process-launcher']:
        # Fixed packaged trampoline, never caller-supplied Python source.
        from mycelium_node.process import _LAUNCHER_SOURCE
        sys.argv=sys.argv[1:]
        exec(compile(_LAUNCHER_SOURCE, '<node-process-launcher>', 'exec'), {'__name__':'__main__'})
        return 126
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
    args=parser.parse_args()
    app=None
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
        except DemoError as error:
            if str(error) != 'already_running': raise
            launch=reopen_native(args.state_root)
            if not args.headless: webbrowser.open(launch.as_uri())
            print(json.dumps({'event':'private_browser_reopened','model_contract':model_contract()})); return 0
        done=threading.Event()
        def stop(signum,frame): done.set()
        signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        if not args.headless: webbrowser.open(app.launch_file.as_uri())
        print(json.dumps({'event':'launcher_started','distribution':'unsigned_development_demo','serving_role':'not_yet_serving','model_contract':model_contract()}),flush=True)
        while not done.wait(.25) and not app.closed.is_set(): pass
        return 0
    except DemoError as error:
        print(json.dumps({'event':'launcher_refused','code':str(error),
            **({'observed_platform':error.observed_platform} if hasattr(error,'observed_platform') else {})}),file=sys.stderr)
        return 2
    except Exception as error:
        # Never expose exception strings that might carry request/state material.
        # The bounded locus (root-relative, allowlisted) names WHICH state file
        # failed without leaking an absolute path or any exception text.
        locus=bounded_failure_locus(error,args.state_root)
        print(json.dumps({'event':'launcher_failed','code':public_failure_code(error),'error_type':type(error).__name__,**locus,'state_retained':True}),file=sys.stderr)
        return 3
    finally:
        if app is not None: app.close()

if __name__=='__main__': raise SystemExit(main())
