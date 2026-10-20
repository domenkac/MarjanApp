"""PyInstaller script entrypoint (absolute imports also work from source)."""

from desktop.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
