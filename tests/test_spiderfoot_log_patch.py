"""Exercise upstream queue→handler shutdown without importing its optional stack."""

import importlib.util
import logging
import sys
import types
from pathlib import Path
from queue import Queue
from unittest.mock import patch


def load_logger(tmp_path):
    import subprocess

    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "spiderfoot/observability"
    source.mkdir(parents=True)
    path = source / "logger.py"
    path.write_text((root / "tests/fixtures/parity/spiderfoot_logger_before.txt").read_text())
    subprocess.run(
        [
            "git",
            "apply",
            "--include=spiderfoot/observability/logger.py",
            str(root / "deploy/spiderfoot/production-readiness.patch"),
        ],
        cwd=tmp_path,
        check=True,
    )
    writes = []

    class Database:
        def __init__(self, opts):
            pass

        def scanLogEvents(self, batch):
            writes.extend(batch)
            return True

        def close(self):
            pass

    sf = types.ModuleType("spiderfoot")
    sf.SpiderFootDb = Database
    sf.SpiderFootHelpers = types.SimpleNamespace(logPath=lambda: str(tmp_path))
    cfg = types.ModuleType("spiderfoot.observability.logging_config")
    cfg.LOG_FORMAT_DEBUG = cfg.LOG_FORMAT_TEXT = "%(message)s"
    spec = importlib.util.spec_from_file_location("test_sf_logger", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules, {"spiderfoot": sf, "spiderfoot.observability.logging_config": cfg}
    ):
        spec.loader.exec_module(module)
    return module, writes


def test_scan_listener_flushes_even_a_single_log(tmp_path):
    module, writes = load_logger(tmp_path)
    queue = Queue()
    listener = module.logListenerSetup(queue, {"__logging": True})
    record = logging.LogRecord(
        "spiderfoot.sfp_accounts", logging.INFO, "module.py", 1, "safe message", (), None
    )
    record.scanId = "SANITIZED-SCAN"
    queue.put(record)
    listener.stop()
    assert any(row[0] == "SANITIZED-SCAN" for row in writes)
