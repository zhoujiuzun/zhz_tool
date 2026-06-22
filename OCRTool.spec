# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('app/test_image.png', 'app'), ('app/logo.png', 'app'), ('app/logo.ico', 'app'),
           ('app/Everything64.dll', 'app')],
    hiddenimports=['pynput.keyboard._win32', 'pynput.mouse._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# 单目录(onedir)打包:DLL/pyd 永久躺在 dist/zhz_tool/ 里,不再每次启动解压。
# 杀软只在首次扫一遍后记住结论,helper 启动不再被反复全盘扫(治"启动按分钟算")。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # ★ onedir 关键:二进制不塞进 exe,交给下面 COLLECT
    name='zhz_tool',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app/logo.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='zhz_tool',              # 产物在 dist/zhz_tool/ 文件夹
)

