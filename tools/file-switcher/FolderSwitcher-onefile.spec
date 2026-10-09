# -*- mode: python ; coding: utf-8 -*-
# Single-file build of the folder switcher.
# Bundled payload is unpacked to sys._MEIPASS at launch; all user data
# (config, snapshots, journals, logs) stays in %LOCALAPPDATA%, never here.

import os

block_cipher = None
root = os.path.abspath('.')

a = Analysis(
    ['folder_app.py'],
    pathex=[root],
    binaries=[],
    datas=[
        ('folder-switcher.ps1', '.'),
        ('profile_store.py', '.'),
        ('templates/folder-config.template.json', 'templates'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='FolderSwitcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
