"""Build on Apple Silicon using the documented arm64 environment."""

import os
import platform
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

if sys.platform != "darwin" or platform.machine() != "arm64":
    raise SystemExit("Build on native Apple Silicon with arm64 Python.")

root = Path(SPECPATH).parents[1]
native = Path(os.environ["MARJAN_BUILD_NATIVE_LIBS"])
required = (
    "libgobject-2.0.0.dylib", "libpango-1.0.0.dylib", "libharfbuzz.0.dylib",
    "libharfbuzz-subset.0.dylib", "libfontconfig.1.dylib", "libpangoft2-1.0.0.dylib",
)
if not native.is_absolute() or any(not (native / name).is_file() for name in required):
    raise SystemExit("Supply complete arm64 native libraries. See docs/desktop-macos.md.")
packages = (
    "app", "desktop", "core", "companies", "catalogs", "contracts", "invoicing",
    "delivery", "rendering", "archive", "accounting_export", "ui",
)
hidden = ["desktop.settings", "webview.platforms.cocoa", "AppKit", "WebKit", "Foundation", "keyring.backends.macOS"]
datas = []
for package in packages:
    hidden += collect_submodules(package)
    datas += collect_data_files(package, includes=["templates/**", "static/**", "xsd/**"])
for package in ("weasyprint", "keyring", "webview"):
    datas += collect_data_files(package)
hidden += collect_submodules("keyring.backends")
binaries = [(str(path), ".") for path in sorted(native.glob("*.dylib"))]
a = Analysis(
    [str(root / "desktop_entry.py")], pathex=[str(root)], binaries=binaries,
    datas=datas, hiddenimports=hidden,
    runtime_hooks=[str(root / "packaging" / "macos" / "runtime.py")],
    excludes=["PyQt5", "PyQt6", "PySide2", "PySide6", "gtk", "gi", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="MarjanApp", console=False, target_arch="arm64", argv_emulation=False)
collection = COLLECT(exe, a.binaries, a.datas, name="MarjanApp")
app = BUNDLE(
    collection, name="MarjanApp.app", bundle_identifier="si.marjanapp.desktop.test",
    info_plist={
        "CFBundleDisplayName": "MarjanApp TEST", "CFBundleShortVersionString": "0.1.0",
        "NSHighResolutionCapable": True,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
