# SPDX-License-Identifier: AGPL-3.0-only
"""Offline, headless AppKit dispatch against a process owned by this test."""
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != 'darwin', reason='system AppKit requires macOS')
@pytest.mark.parametrize('event', ['appleevent', 'menu'])
def test_real_native_quit_dispatch_defers_exit(tmp_path, event):
    from mycelium_a13_macos.native_quit import build_quit_bridge
    library = build_quit_bridge(tmp_path / 'libmycelium_quit.dylib')
    source = tmp_path / 'probe.m'
    source.write_text(r'''
#import <AppKit/AppKit.h>
#import <Carbon/Carbon.h>
#include <unistd.h>
int mycelium_quit_install(int);
int mycelium_quit_poll(void);
void mycelium_quit_uninstall(void);
int main(int argc, char **argv) {
    @autoreleasepool {
        fputs("before-install\n", stderr);
        if (!mycelium_quit_install(1)) return 10;
        fputs("after-install\n", stderr);
        if (argv[1][0] == 'a') {
            pid_t pid = getpid();
            AEAddressDesc address = {typeNull, NULL};
            AppleEvent event = {typeNull, NULL};
            if (AECreateDesc(typeKernelProcessID, &pid, sizeof(pid), &address)) return 11;
            if (AECreateAppleEvent(kCoreEventClass, kAEQuitApplication, &address,
                                  kAutoGenerateReturnID, kAnyTransactionID, &event)) return 12;
            OSStatus status = AESendMessage(&event, NULL, kAENoReply | kAENeverInteract, 60);
            AEDisposeDesc(&event); AEDisposeDesc(&address);
            if (status) return 13;
        } else {
            NSMenuItem *quit = [[[NSApp mainMenu] itemAtIndex:0].submenu itemAtIndex:0];
            if (![quit.keyEquivalent isEqualToString:@"q"]) return 14;
            [NSApp sendAction:quit.action to:quit.target from:quit];
        }
        NSDate *deadline = [NSDate dateWithTimeIntervalSinceNow:3];
        while (!mycelium_quit_poll()) {
            if ([deadline timeIntervalSinceNow] <= 0) return 15;
            [NSThread sleepForTimeInterval:0.01];
        }
        // A second quit is harmless and cannot cause AppKit to exit first.
        [NSApp terminate:nil];
        if (!mycelium_quit_poll()) return 16;
        mycelium_quit_uninstall();
        puts("quit_deferred_for_cleanup");
        return 0;
    }
}
''')
    executable = tmp_path / 'probe'
    subprocess.run(['/usr/bin/xcrun', 'clang', '-fobjc-arc', '-framework', 'AppKit',
                    '-framework', 'Carbon', str(source), str(library), '-o', str(executable)],
                   capture_output=True, check=True, timeout=60)
    try:
        result = subprocess.run([str(executable), event], capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired as error:
        pytest.fail(f'native probe timed out: {error.stderr!r}')
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert result.stdout.strip() == 'quit_deferred_for_cleanup'


@pytest.mark.skipif(sys.platform != 'darwin', reason='system AppKit requires macOS')
def test_python_bridge_receives_external_pid_targeted_quit(tmp_path):
    from mycelium_a13_macos.native_quit import build_quit_bridge, LIBRARY_NAME
    from native_quit_sender import send_quit
    import queue
    import threading
    build_quit_bridge(tmp_path / 'mycelium_a13_macos' / 'native' / LIBRARY_NAME)
    code = '''
import sys, time
sys._MEIPASS = sys.argv[1]
from mycelium_a13_macos.native_quit import NativeQuit
requested = []
bridge = NativeQuit(lambda: requested.append(True), headless=True)
print('ready', flush=True)
deadline = time.monotonic() + 5
while not requested and time.monotonic() < deadline:
    bridge.poll()
    time.sleep(.01)
bridge.close()
assert requested
print('cleanup_allowed', flush=True)
'''
    process = subprocess.Popen([sys.executable, '-B', '-c', code, str(tmp_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        assert lines.get(timeout=10).strip() == 'ready'
        send_quit(process.pid)
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert output.strip() == 'cleanup_allowed'
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
