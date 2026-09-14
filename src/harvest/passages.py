"""Bounded lexical retrieval over one adapter's text, with exact character coordinates."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass

from .store import digest

VERSION = "fts5-passages/1"
WINDOW = 2600
OVERLAP = 400
MAX_PASSAGES = 5
INPUT_LIMIT = 14000
SEPARATOR = "\n\n[... omitted source text ...]\n\n"


@dataclass(frozen=True)
class Selection:
    text: str
    spans: list[dict]
    metadata: dict

    def locate(self, quote: str) -> int | None:
        # Never accept an apparent quote spanning two disconnected passages or a marker.
        for span in self.spans:
            part = self.text[span["input_start"] : span["input_end"]]
            if quote in part:
                return span["start"] + part.index(quote)
        return None


def queries_for(objective, fields, context):
    """Treat every operator/model-proposed term as literal data, never FTS syntax."""
    missing = set(context.get("missing_fields", []))
    ordered_fields = [f for f in fields if f in missing] + [f for f in fields if f not in missing]
    candidates = ordered_fields[:8] + [objective]
    for previous in context.get("previous_decisions", [])[:2]:
        candidates.extend(previous.get("gaps", [])[:2])
    queries = []
    for candidate in candidates:
        terms = list(dict.fromkeys(re.findall(r"[^\W_]+", str(candidate).casefold())))[:32]
        if terms:
            query = " OR ".join('"' + term + '"' for term in terms)
            if query not in queries:
                queries.append(query)
    return queries[:13]


def rank_windows(windows, queries):
    # A per-attempt ephemeral index avoids another persistent schema/cache to invalidate.
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE VIRTUAL TABLE passages USING fts5(text, tokenize='unicode61')")
        db.executemany(
            "INSERT INTO passages(rowid,text) VALUES(?,?)",
            ((i, text) for i, (_, text) in enumerate(windows)),
        )
        return [
            [
                row[0]
                for row in db.execute(
                    "SELECT rowid FROM passages WHERE passages MATCH ? ORDER BY bm25(passages),rowid",
                    (query,),
                )
            ]
            for query in queries
        ]


def shown_fraction(start: int, end: int, exclude) -> float:
    covered = sum(max(0, min(end, e) - max(start, s)) for s, e in exclude)
    return covered / max(1, end - start)


def select_passages(
    text: str,
    objective: str,
    fields: list[str],
    context: dict,
    *,
    exclude=(),
    reserve_ends: bool = True,
) -> Selection:
    """`exclude` holds (start, end) adapter-text ranges already shown in earlier passes."""
    queries = queries_for(objective, fields, context)
    backend = "whole-text"
    excluded_count = 0
    if len(text) <= INPUT_LIMIT:
        selected = [] if exclude and shown_fraction(0, len(text), exclude) >= 0.5 else [(0, text)]
        excluded_count = 1 - len(selected)
    else:
        starts = list(range(0, len(text) - WINDOW, WINDOW - OVERLAP)) + [len(text) - WINDOW]
        windows = [(i, text[i : i + WINDOW]) for i in starts]
        # A window mostly shown in an earlier pass is not a candidate; the budget goes to unseen text.
        available = [
            i
            for i, (start, part) in enumerate(windows)
            if not exclude or shown_fraction(start, start + len(part), exclude) < 0.5
        ]
        excluded_count = len(windows) - len(available)
        # Keep introduction and conclusion even when their vocabulary differs from the query.
        chosen = [i for i in (0, len(windows) - 1) if reserve_ends and i in available]
        chosen = list(dict.fromkeys(chosen))
        backend = "sqlite-fts5-bm25"
        try:
            allowed = set(available)
            rankings = [[i for i in r if i in allowed] for r in rank_windows(windows, queries)]
        except sqlite3.OperationalError as exc:
            if "no such module: fts5" not in str(exc):
                raise
            # SQLite builds without FTS5 still run, with explicit reduced retrieval capability.
            rankings = []
            backend = "coverage-fallback-no-fts5"
        # Round-robin requested fields before broad objectives; repeated boilerplate cannot
        # consume every slot solely because it matches one field many times.
        while len(chosen) < MAX_PASSAGES and any(rankings):
            for ranking in rankings:
                while ranking and ranking[0] in chosen:
                    ranking.pop(0)
                if ranking and len(chosen) < MAX_PASSAGES:
                    chosen.append(ranking.pop(0))
        # No lexical matches: sample the interior deterministically instead of another prefix.
        for fraction in (0.5, 0.25, 0.75):
            index = round((len(windows) - 1) * fraction)
            if index in allowed and index not in chosen and len(chosen) < MAX_PASSAGES:
                chosen.append(index)
        # Later passes with spare slots read unseen text in document order. Pass 1 always
        # fills its slots above, so this never alters single-pass selection.
        for index in available:
            if len(chosen) >= MAX_PASSAGES:
                break
            if index not in chosen:
                chosen.append(index)
        selected = [windows[i] for i in sorted(chosen)]

    # Merge overlapping/adjacent windows to retain contiguous quotes across their boundaries.
    ranges = []
    for start, part in selected:
        end = start + len(part)
        if ranges and start <= ranges[-1][1]:
            ranges[-1][1] = max(ranges[-1][1], end)
        else:
            ranges.append([start, end])
    parts, spans, cursor = [], [], 0
    for start, end in ranges:
        if parts:
            parts.append(SEPARATOR)
            cursor += len(SEPARATOR)
        part = text[start:end]
        parts.append(part)
        spans.append(
            {"start": start, "end": end, "input_start": cursor, "input_end": cursor + len(part)}
        )
        cursor += len(part)
    source_text = "".join(parts)
    assert len(source_text) <= INPUT_LIMIT
    selected_chars = sum(end - start for start, end in ranges)
    return Selection(
        source_text,
        spans,
        {
            "version": VERSION,
            "backend": backend,
            "queries": queries,
            "normalized_text_sha256": digest(text),
            "normalized_chars": len(text),
            "selected_chars": selected_chars,
            "omitted_chars": len(text) - selected_chars,
            "excluded_span_count": excluded_count,
            "spans": spans,
            "coordinate_unit": "unicode-code-point; end-exclusive; adapter text",
        },
    )
