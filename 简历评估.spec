# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['/Users/yaoxiujie/.cherrystudio/install/global/resume_evaluator/main.py'],
    pathex=[],
    binaries=[],
    datas=[('config.yaml', '.'), ('prompts', 'prompts'), ('rules', 'rules')],
    hiddenimports=['watchdog.observers.polling'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'pandas'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='简历评估',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
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
    name='简历评估',
)
app = BUNDLE(
    coll,
    name='简历评估.app',
    icon=None,
    bundle_identifier=None,
)
