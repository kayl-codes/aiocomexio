"""Check a built distribution before it is published (ci.yml on every PR, pypi.yaml on the release build).

The required files come from git, not a hand-kept list: every tracked file of the package must reach the wheel,
and the sdist must also carry tests/ and scripts/ so the reference catalogs can be rebuilt from it. The wheel is
then installed into a fresh venv, every module is imported and the reference catalogs must all load — the
catalog loader itself only logs a missing or broken file.

With --version (pypi.yaml passes the release tag), the built version must match it, so a tag that was cut before
the version bump cannot publish the previous version under the new release.

Usage (from a checkout, after `python -m build`):
    python scripts/check_dist.py [dist_dir] [--version X.Y.Z]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = "src/aiocomexio"
SDIST_PATHS = ("src", "tests", "scripts", "README.md", "LICENSE")
WHEEL_LICENSE_SUFFIX = ".dist-info/licenses/LICENSE"

SMOKE_TEST = """
import importlib
import sys

from aiocomexio.reference_catalog import LIVE_EXTRACTORS, REFERENCE_DIR, load_reference_catalogs

for name in sys.argv[1:]:
    importlib.import_module(name)
if "site-packages" not in str(REFERENCE_DIR):
    sys.exit(f"aiocomexio imported from {REFERENCE_DIR}, not from the installed wheel")
catalogs = load_reference_catalogs()
if set(catalogs) != set(LIVE_EXTRACTORS):
    sys.exit(f"reference catalogs: loaded {sorted(catalogs)}, expected {sorted(LIVE_EXTRACTORS)}")
print(f"{len(sys.argv) - 1} modules import, reference catalogs load: {sorted(catalogs)}")
"""


def _tracked(*paths: str) -> set[str]:
    result = subprocess.run(["git", "ls-files", "--", *paths], cwd=ROOT, check=True, capture_output=True, text=True)
    if not (files := set(result.stdout.splitlines())):
        raise SystemExit(f"git ls-files found nothing under {paths} in {ROOT}")
    return files


def _single(dist: Path, pattern: str) -> Path:
    found = sorted(dist.glob(pattern))
    if len(found) != 1:
        raise SystemExit(f"{dist}: expected exactly one {pattern}, found {[path.name for path in found]}")
    return found[0]


def _missing_files(sdist: Path, wheel: Path) -> list[str]:
    with tarfile.open(sdist) as tar:
        # Every sdist entry sits below one aiocomexio-<version>/ directory.
        sdist_files = {name.split("/", 1)[1] for name in tar.getnames() if "/" in name}
    with zipfile.ZipFile(wheel) as whl:
        wheel_files = set(whl.namelist())
    missing = [f"{sdist.name}: {path}" for path in sorted(_tracked(*SDIST_PATHS) - sdist_files)]
    package_files = {path.removeprefix("src/") for path in _tracked(PACKAGE_DIR)}
    missing += [f"{wheel.name}: {path}" for path in sorted(package_files - wheel_files)]
    if not any(name.endswith(WHEEL_LICENSE_SUFFIX) for name in wheel_files):
        missing.append(f"{wheel.name}: *{WHEEL_LICENSE_SUFFIX}")
    return missing


def _module_names() -> list[str]:
    """Every tracked module, also in a subdirectory without __init__.py that pkgutil.walk_packages would skip."""
    names = []
    for path in sorted(_tracked(PACKAGE_DIR)):
        if path.endswith(".py"):
            parts = Path(path).relative_to("src").with_suffix("").parts
            names.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _smoke_test(wheel: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_dir = Path(tmp) / "venv"
        venv.create(env_dir, with_pip=True)
        python = env_dir / ("Scripts" if os.name == "nt" else "bin") / "python"
        pip = [str(python), "-m", "pip", "--disable-pip-version-check", "--quiet"]
        subprocess.run([*pip, "install", "--only-binary", ":all:", str(wheel.resolve())], check=True)
        # cwd outside the checkout, so `import aiocomexio` cannot resolve to the source tree.
        subprocess.run([str(python), "-c", SMOKE_TEST, *_module_names()], cwd=tmp, check=True)


def main(dist: Path, version: str | None) -> None:
    sdist = _single(dist, "aiocomexio-*.tar.gz")
    wheel = _single(dist, "aiocomexio-*.whl")
    if version is not None:
        # packaging comes with build; it normalises a tag like 0.4.0-rc1 to the built 0.4.0rc1.
        from packaging.version import Version

        expected = str(Version(version))
        if sdist.name != f"aiocomexio-{expected}.tar.gz" or not wheel.name.startswith(f"aiocomexio-{expected}-"):
            raise SystemExit(f"{sdist.name} / {wheel.name} do not match the release version {version}")
    if missing := _missing_files(sdist, wheel):
        raise SystemExit("Missing from the distribution:\n" + "\n".join(missing))
    print(f"{sdist.name} and {wheel.name} contain every tracked file", flush=True)
    _smoke_test(wheel)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dist", nargs="?", type=Path, default=Path("dist"))
    parser.add_argument("--version", help="expected package version, e.g. the release tag without its v")
    args = parser.parse_args()
    main(args.dist, args.version)
