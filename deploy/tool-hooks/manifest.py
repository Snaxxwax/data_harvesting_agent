import hashlib
import json
import os
import subprocess
from pathlib import Path

manifest = {
    "schema": 1,
    "revision": os.environ.get("HARVEST_BUILD_REVISION", "unrecorded"),
    "patches": {},
    "tools": {},
}
for path in sorted(Path("/opt/harvest-hooks").glob("*")):
    if path.is_file():
        manifest["patches"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
for name in ("maigret", "ghunt"):
    python = Path("/opt/uv-tools") / name / "bin/python"
    if not python.exists():
        continue
    code = "import importlib.metadata,json; print(json.dumps({d.metadata['Name']:d.version for d in importlib.metadata.distributions()}))"
    manifest["tools"][name] = json.loads(subprocess.check_output([str(python), "-c", code]))
    if name == "maigret":
        code = """import json,hashlib,sys
from maigret.sites import MaigretDatabase
from maigret.db_updater import BUNDLED_DB_PATH
p=BUNDLED_DB_PATH
sites=MaigretDatabase().load_from_path(p).ranked_sites_dict(top=sys.maxsize, id_type='username', disabled=False)
print(json.dumps({'eligible_sites':len(sites),'database_sha256':hashlib.sha256(open(p,'rb').read()).hexdigest()}))"""
        manifest["maigret"] = json.loads(subprocess.check_output([str(python), "-c", code]))
Path("/app/release-manifest.json").write_text(json.dumps(manifest, indent=2))
