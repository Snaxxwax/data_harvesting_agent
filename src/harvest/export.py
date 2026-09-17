"""Record-oriented CSV projection of `Store.job_records`, alongside the existing evidence-rich
JSONL export. Headers are deterministic for a given job's data: two leading identity columns,
then three columns per field (value, status, sources) in sorted field-name order.
"""

from __future__ import annotations

import csv
import io
import json

_UNSAFE_LEADING = ("=", "+", "-", "@", "\t", "\r")


def _cell(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if text and text[0] in _UNSAFE_LEADING:
        # Neutralize spreadsheet formula injection (CSV is opened in Excel/Sheets far more
        # often than parsed programmatically); a leading apostrophe forces literal text.
        text = "'" + text
    return text


def render_csv(records: list[dict]) -> str:
    fields = sorted({field for r in records for field in r["fields"]})
    header = ["entity_id", "entity_key"]
    for f in fields:
        # Field names are user-controlled (JobSpec.fields); neutralize header cells the
        # same as data cells, not just the raw name -- the derived __status/__sources
        # column names share its unsafe leading character too.
        header += [_cell(f), _cell(f"{f}__status"), _cell(f"{f}__sources")]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in records:
        row = [_cell(r["entity_id"]), _cell(r["entity_key"])]
        for f in fields:
            info = r["fields"].get(f)
            if info is None or info.get("missing"):
                row += ["", "missing", ""]
                continue
            sources = ",".join(sorted({c["source_url"] for c in info["candidates"]}))
            if info["conflict"]:
                row += ["", "conflict", _cell(sources)]
            else:
                row += [_cell(info["value"]), "ok", _cell(sources)]
        writer.writerow(row)
    return buf.getvalue()
