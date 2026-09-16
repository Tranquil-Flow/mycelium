# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo/mycelium_a13_macos/__main__.py'],
    pathex=['/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo'],
    binaries=[],
    datas=[('/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo/mycelium_a13_macos/web', 'mycelium_a13_macos/web')],
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
