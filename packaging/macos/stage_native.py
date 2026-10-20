"""Copy the Homebrew dylib dependency closure into a new private staging directory."""

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def output(*arguments):
    return subprocess.check_output(arguments, text=True).strip()


def stage(destination):
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise SystemExit("Native arm64 macOS is required.")
    prefix = Path(output("brew", "--prefix"))
    seeds = {
        "glib": ("libgobject-2.0.0.dylib",),
        "pango": ("libpango-1.0.0.dylib", "libpangoft2-1.0.0.dylib"),
        "harfbuzz": ("libharfbuzz.0.dylib", "libharfbuzz-subset.0.dylib"),
        "fontconfig": ("libfontconfig.1.dylib",),
    }
    pending = [prefix / "opt" / formula / "lib" / name for formula, names in seeds.items() for name in names]
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    collected = {}
    while pending:
        library = pending.pop()
        source = library.resolve(strict=True)
        if not source.is_relative_to(prefix):
            raise ValueError("Only Homebrew native libraries may be staged.")
        if "arm64" not in output("lipo", "-archs", str(source)).split():
            raise ValueError(f"Missing arm64 architecture: {source.name}")
        name = library.name
        if name in collected:
            if collected[name] != source:
                raise ValueError(f"Conflicting library basename: {name}")
            continue
        collected[name] = source
        shutil.copy2(source, destination / name)
        for line in output("otool", "-L", str(source)).splitlines()[1:]:
            dependency = line.strip().split(" (", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if dependency.startswith("@"):
                raise ValueError(f"Resolve non-absolute install name before building: {dependency}")
            pending.append(Path(dependency))
    print(f"Staged {len(collected)} libraries. Retain brew versions and pip freeze with build records.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    stage(parser.parse_args().destination)
