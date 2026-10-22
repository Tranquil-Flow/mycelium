# SPDX-License-Identifier: AGPL-3.0-only
"""In-memory extended attributes; no real xattr write, read, or removal.

Inode keys preserve fake attributes over atomic rename. Copy propagation is
explicit, not a claim about macOS metadata semantics. The shell adapter uses a
private loopback server so subprocesses share this same in-memory dictionary.
"""
from __future__ import annotations

import json
from pathlib import Path
import socketserver
import subprocess
import sys
import threading

QUARANTINE = "com.apple.quarantine"  # fake dictionary key only


class FakeXattrs:
    def __init__(self):
        self.values = {}
        self.calls = []
        self.fail_delete = False

    def attribute(self, path, value=None, *, write=None, link=True):
        path = Path(path)
        try:
            stat = path.lstat() if link else path.stat()
        except FileNotFoundError:
            return None
        key = (stat.st_dev, stat.st_ino, QUARANTINE)
        value = write if write is not None else value
        if value is not None:
            self.values[key] = value
        return self.values.get(key)

    def copy(self, source, destination):
        # Real byte/link copy, fake metadata propagation; never invoke xattr.
        code = subprocess.run(["/usr/bin/ditto", str(source), str(destination)], check=False).returncode
        if code == 0:
            for path in [source, *source.rglob("*")]:
                value = self.attribute(path, link=True)
                if value is not None:
                    self.attribute(destination / path.relative_to(source), value, link=True)
        return code

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        assert argv[0] == "/usr/bin/xattr" and argv[-2] == QUARANTINE
        path = Path(argv[-1])
        if not path.exists():
            return subprocess.CompletedProcess(argv, 1, "", "missing")
        if "-d" in argv:
            if self.fail_delete:
                return subprocess.CompletedProcess(argv, 1, "", "denied")
            for item in [path, *path.rglob("*")]:
                stat = item.lstat()
                self.values.pop((stat.st_dev, stat.st_ino, QUARANTINE), None)
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert "-p" in argv
        value = self.attribute(path, link="-s" in argv)
        return subprocess.CompletedProcess(argv, int(value is None), value or "", "")

    def shell_adapter(self, directory):
        fake = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                argv = json.loads(self.rfile.readline())
                result = fake.run(["/usr/bin/xattr", *argv])
                self.wfile.write(json.dumps([result.returncode, result.stdout]).encode() + b"\n")

        server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = directory / "fake-xattr"
        client.write_text(
            f"#!{sys.executable}\n"
            "import json, socket, sys\n"
            f"with socket.create_connection({server.server_address!r}, timeout=10) as conn:\n"
            "    conn.sendall(json.dumps(sys.argv[1:]).encode() + b'\\n')\n"
            "    status, output = json.loads(conn.makefile('rb').readline())\n"
            "print(output)\n"
            "sys.exit(status)\n"
        )
        client.chmod(0o700)
        return client, server, thread
