# SPDX-License-Identifier: AGPL-3.0-only
"""Where the packaged macOS app is running from: installed, translocated, or somewhere else.

macOS 26 "Open Anyway" for the app launches the *bundle itself*, translocated (a random read-only
``AppTranslocation`` path) and without ``--install``.  A normal session started there is not an install:
its state and its Applications entry would belong to a throw-away copy.  A bare launch of the frozen app
from anywhere but ``programs/<version>`` therefore refuses and tells the user to run
``Install Mycelium.command`` (``docs/design/MACOS-INSTALL-FLOW.md``).  Only a bare launch is judged: any
explicit flag (``--headless``, ``--state-root``, ``--install``, ``--node-process-launcher``...) keeps its
meaning, so node processes, services and gates are unaffected.

Stdlib only and import-cheap: it runs before the app is imported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

CODE = "not_installed"
MESSAGE = ("Mycelium is not installed yet, so it did not start. Open the folder you downloaded and "
           "double-click 'Install Mycelium.command' (macOS asks once to confirm it in System Settings > "
           "Privacy & Security > Open Anyway). After that, open 'Mycelium A13' from the Applications folder "
           "in your home folder.")


def programs_root(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Application Support" / "Mycelium" / "programs"


def classify(executable: Path, root: Path) -> str:
    """``installed`` below ``root`` (links resolved), ``translocated`` under AppTranslocation, else ``elsewhere``."""

    resolved = Path(executable).resolve()
    if "AppTranslocation" in resolved.parts:
        return "translocated"
    base = Path(root).resolve()
    return "installed" if resolved.is_relative_to(base) else "elsewhere"


def _bare(argv: list[str]) -> bool:
    # Finder passes -psn_<serial> on some macOS versions; that is not a request for any mode.
    return all(argument.startswith("-psn_") for argument in argv[1:])


def not_installed_refusal(*, frozen: bool, platform: str, argv: list[str], executable: Path,
                          programs_root: Path) -> dict[str, Any] | None:
    """The public refusal for a bare launch outside the install, or ``None`` (no paths in it)."""

    if not frozen or platform != "darwin" or not _bare(argv):
        return None
    location = classify(executable, programs_root)
    if location == "installed":
        return None
    return {"event": "launcher_refused", "code": CODE, "location": location, "message": MESSAGE}
