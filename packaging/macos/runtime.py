"""Resolve WeasyPrint CFFI libraries from the bundle without runtime Homebrew."""

import sys
from pathlib import Path

if sys.platform == "darwin" and getattr(sys, "frozen", False):
    import cffi

    _original_dlopen = cffi.FFI.dlopen
    _libraries = {
        "gobject-2.0": "libgobject-2.0.0.dylib",
        "pango-1.0": "libpango-1.0.0.dylib",
        "harfbuzz": "libharfbuzz.0.dylib",
        "harfbuzz-subset": "libharfbuzz-subset.0.dylib",
        "harfbuzz-vector": "libharfbuzz-vector.0.dylib",
        "fontconfig": "libfontconfig.1.dylib",
        "pangoft2-1.0": "libpangoft2-1.0.0.dylib",
    }

    def _bundled_dlopen(self, name, flags=0):
        if isinstance(name, str):
            for short, filename in _libraries.items():
                aliases = {
                    short, "lib" + short, filename, "lib" + short + ".dylib",
                    short + "-0", "lib" + short + "-0", short + "-0.0",
                    short + "-1", "lib" + short + "-1",
                    "lib" + short + ".so.0", "lib" + short + ".so.1",
                    "lib" + short + "-0.dll", "lib" + short + "-1.dll",
                }
                if name in aliases:
                    name = str(Path(sys._MEIPASS) / filename)
                    break
        return _original_dlopen(self, name, flags)

    cffi.FFI.dlopen = _bundled_dlopen
