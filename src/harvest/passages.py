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


def select_passages(text: str, objective: str, fields: list[str], context: dict) -> Selection:
    queries = queries_for(objective, fields, context)
    backend = "whole-text"
    if len(text) <= INPUT_LIMIT:
        selected = [(0, text)]
    else:
        starts = list(range(0, len(text) - WINDOW, WINDOW - OVERLAP)) + [len(text) - WINDOW]
        windows = [(i, text[i : i + WINDOW]) for i in starts]
        # Keep introduction and conclusion even when their vocabulary differs from the query.
        chosen = [0, len(windows) - 1]
        backend = "sqlite-fts5-bm25"
        try:
            rankings = rank_windows(windows, queries)
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
            if index not in chosen and len(chosen) < MAX_PASSAGES:
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
            "spans": spans,
            "coordinate_unit": "unicode-code-point; end-exclusive; adapter text",
        },
    )
