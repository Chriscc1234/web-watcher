"""
Cut a Web Watcher release: zip the app code, hash it, and write release notes.

    python build_release.py

Produces (in dist/):
  • web-watcher-<version>.zip   — the code bundle (top-level web_watcher/), what the
                                   in-app updater downloads and applies. NO models/data.
  • RELEASE_NOTES_<version>.md  — this version's CHANGELOG section + a `sha256: <hex>` line
                                   (the updater reads that line to verify the download).

Then publish a GitHub Release tagged v<version>, paste the notes as the body, and attach
the zip. The `gh` CLI makes this one command (printed at the end).

The updater only ships CODE. If a release needs new Python deps or new models, say so in
the notes and have the user re-run install.py — updates never touch dependencies.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from web_watcher import updater          # ROOT_FILES: the root scripts that ship in the bundle

ROOT = Path(__file__).resolve().parent
PKG  = ROOT / "web_watcher"
DIST = ROOT / "dist"


def _version() -> str:
    text = (PKG / "__version__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit("could not read __version__")
    return m.group(1)


def _changelog_section(version: str) -> str:
    """The CHANGELOG block for this version (between its heading and the next `## [`)."""
    cl = ROOT / "CHANGELOG.md"
    if not cl.exists():
        return f"Web Watcher {version}"
    text = cl.read_text(encoding="utf-8")
    m = re.search(rf"(##\s*\[{re.escape(version)}\].*?)(?=\n##\s*\[|\Z)", text, re.S)
    return (m.group(1).strip() if m else f"Web Watcher {version}")


def _zip_package(version: str) -> Path:
    DIST.mkdir(exist_ok=True)
    out = DIST / f"web-watcher-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for p in PKG.rglob("*"):
            if p.is_dir() or "__pycache__" in p.parts or p.suffix in (".pyc", ".pyo"):
                continue
            z.write(p, p.relative_to(ROOT))   # arcname keeps the top-level web_watcher/ prefix
        # Root-level scripts the app folder needs. Without these in the bundle, a bug in the
        # launcher (the thing that APPLIES updates) could only ever be fixed by reinstalling.
        for name in updater.ROOT_FILES:
            p = ROOT / name
            if p.exists():
                z.write(p, name)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    version = _version()
    zip_path = _zip_package(version)
    digest = _sha256(zip_path)
    notes = _changelog_section(version) + f"\n\nsha256: {digest}\n"
    notes_path = DIST / f"RELEASE_NOTES_{version}.md"
    notes_path.write_text(notes, encoding="utf-8")

    size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"\n  Built {zip_path.name}  ({size_mb:.1f} MB)")
    print(f"  sha256: {digest}")
    print(f"  notes:  {notes_path.name}")
    print("\n  Publish the GitHub Release (with gh CLI):")
    print(f'    gh release create v{version} "{zip_path}" '
          f'--title "v{version}" --notes-file "{notes_path}"')
    print("  …or create it in the GitHub UI: tag v" + version
          + ", paste the notes as the body, attach the zip.\n")


if __name__ == "__main__":
    main()
