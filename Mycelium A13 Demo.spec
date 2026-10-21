# -*- mode: python ; coding: utf-8 -*-
# THIS SPEC BUILDS THE TREE IT LIVES IN, resolved at build time, and never a hardcoded checkout.

# As generated, this file pointed at <home>/playground/mycelium-a13-macos-demo -- a DIFFERENT git
# checkout that held only a stale copy of the app package, untracked. Nothing invoked that spec, so no bundle
# was ever built from it; but anyone who ran PyInstaller on it would have shipped an app from sources that
# were not the app's sources, and the resulting bundle would still have matched its own digests (measured
# 2026-09-19; ERRATA-6 in the program dir). SPECPATH is the directory containing this file, so this spec can
# only ever build the tree it is committed in.
import os

ROOT = os.path.abspath(SPECPATH)
APP = os.path.join(ROOT, 'mycelium_a13_macos')

a = Analysis(
    [os.path.join(APP, '__main__.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(APP, 'web'), 'mycelium_a13_macos/web')],
    hiddenimports=['mycelium_a13_macos.serving'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Mycelium A13 Demo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch='arm64',
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Mycelium A13 Demo',
)
app = BUNDLE(
    coll,
    name='Mycelium A13 Demo.app',
    icon=None,
    bundle_identifier='org.mycelium.a13-macos-demo',
)
