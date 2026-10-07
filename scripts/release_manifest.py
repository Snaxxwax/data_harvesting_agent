#!/usr/bin/env python3
"""Record the complete local source content, deployment artifacts and validation status."""

import hashlib
import json
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    files = {}
    for folder in ("src", "deploy", "scripts", "tests"):
        for path in sorted((root / folder).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("Dockerfile", "compose.yaml", "pyproject.toml", "uv.lock"):
        files[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    content_hash = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    report = {
        "version": 1,
        "base_revision": revision,
        "source_sha256": content_hash,
        "files": files,
        "release_allowed": False,
        "reason": "all production gates must pass before rollout",
    }
    path = root / "docs/releases/source-manifest.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(revision[:12] + "-" + content_hash[:12])


if __name__ == "__main__":
    main()
