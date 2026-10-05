"""Regenerate src/aiocomexio/reference/<kind>.json from a raw Comexio admin config.

The input is RawConfig.variables of a reference server (ComexioClient.get_raw_config()) saved as
JSON. Only the firmware catalog parts are read from it — no installation data ends up in the output.

Usage (after `pip install -e ".[dev]"`):
    python scripts/build_reference_catalog.py <raw_config.json> <comexio_version>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aiocomexio.reference_catalog import LIVE_EXTRACTORS, REFERENCE_DIR, build_reference

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REFERENCE_DIR = ROOT / "src" / "aiocomexio" / "reference"


def main(raw_path: Path, comexio_version: str) -> None:
    if REFERENCE_DIR.resolve() != SOURCE_REFERENCE_DIR:
        # Any other install (also a regular one into ROOT/.venv) would write into site-packages.
        raise SystemExit(f"aiocomexio is not installed editable from {ROOT} (REFERENCE_DIR is {REFERENCE_DIR})")
    # Developer tool: raw_path is the maintainer's own CLI argument, read-only, no untrusted input.
    raw_config = json.loads(raw_path.read_text(encoding="utf-8"))  # NOSONAR
    if not isinstance(raw_config, dict):
        raise SystemExit(f"{raw_path} must hold a JSON object (RawConfig.variables), got {type(raw_config).__name__}")
    # Build every catalog before writing any, so a refused one leaves the files untouched.
    contents = {kind: build_reference(kind, raw_config, comexio_version) for kind in LIVE_EXTRACTORS}
    for kind, content in contents.items():
        out = SOURCE_REFERENCE_DIR / f"{kind}.json"
        out.write_text(json.dumps(content, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"{out.relative_to(ROOT)}: {len(content['entries'])} entries, {out.stat().st_size} bytes")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), sys.argv[2])
