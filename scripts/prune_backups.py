from __future__ import annotations

import re
import sys
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

PATTERN = re.compile(r"^harvest-(\d{8}T\d{6}Z)\.tar\.gz\.age$")


def main() -> None:
    root = Path(sys.argv[1]).expanduser().resolve()
    items: list[tuple[datetime, Path]] = []
    for path in root.glob("harvest-*.tar.gz.age"):
        match = PATTERN.match(path.name)
        if match:
            items.append(
                (
                    datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
                        tzinfo=UTC
                    ),
                    path,
                )
            )
    items.sort(reverse=True)
    if not items:
        return

    keep: set[Path] = {items[0][1]}

    def newest_by(key, limit: int) -> None:
        buckets: OrderedDict[object, Path] = OrderedDict()
        for when, path in items:
            bucket = key(when)
            if bucket not in buckets:
                buckets[bucket] = path
        keep.update(list(buckets.values())[:limit])

    newest_by(lambda d: d.date(), 7)
    newest_by(lambda d: d.isocalendar()[:2], 4)
    newest_by(lambda d: (d.year, d.month), 3)

    for _, path in items:
        if path in keep:
            continue
        for candidate in (
            path,
            Path(str(path) + ".sha256"),
            Path(str(path) + ".backup-id"),
        ):
            candidate.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
