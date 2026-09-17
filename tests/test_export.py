import csv
import io

import pytest

from harvest.export import render_csv


def records():
    return [
        {
            "entity_id": "e1",
            "entity_key": "alpha",
            "fields": {
                "name": {
                    "value": "Alpha",
                    "conflict": False,
                    "candidates": [{"source_url": "https://example.org/a"}],
                },
                "price": {
                    "value": None,
                    "conflict": True,
                    "candidates": [
                        {"source_url": "https://example.org/a"},
                        {"source_url": "https://example.org/b"},
                    ],
                },
            },
        },
        {
            "entity_id": "e2",
            "entity_key": "beta",
            "fields": {
                "name": {
                    "value": "Beta",
                    "conflict": False,
                    "candidates": [{"source_url": "https://example.org/c"}],
                },
            },
        },
    ]


def test_headers_are_deterministic_and_sorted():
    text = render_csv(records())
    header = next(csv.reader(io.StringIO(text)))
    assert header == [
        "entity_id",
        "entity_key",
        "name",
        "name__status",
        "name__sources",
        "price",
        "price__status",
        "price__sources",
    ]


def test_conflict_row_has_empty_value_and_conflict_status():
    rows = list(csv.DictReader(io.StringIO(render_csv(records()))))
    alpha = next(r for r in rows if r["entity_key"] == "alpha")
    assert alpha["price"] == ""
    assert alpha["price__status"] == "conflict"
    assert "example.org/a" in alpha["price__sources"]
    assert "example.org/b" in alpha["price__sources"]


def test_missing_field_row_is_explicit():
    rows = list(csv.DictReader(io.StringIO(render_csv(records()))))
    beta = next(r for r in rows if r["entity_key"] == "beta")
    assert beta["price"] == ""
    assert beta["price__status"] == "missing"
    assert beta["price__sources"] == ""


def test_ok_row_has_value_and_sources():
    rows = list(csv.DictReader(io.StringIO(render_csv(records()))))
    alpha = next(r for r in rows if r["entity_key"] == "alpha")
    assert alpha["name"] == "Alpha"
    assert alpha["name__status"] == "ok"
    assert alpha["name__sources"] == "https://example.org/a"


def test_formula_injection_is_neutralized():
    poisoned = [
        {
            "entity_id": "=cmd|' /C calc'!A0",
            "entity_key": '=HYPERLINK("http://evil")',
            "fields": {},
        }
    ]
    text = render_csv(poisoned)
    body_row = list(csv.reader(io.StringIO(text)))[1]
    assert body_row[0].startswith("'=")
    assert body_row[1].startswith("'=")


def test_no_records_still_has_identity_header():
    assert render_csv([]).strip() == "entity_id,entity_key"


def test_header_cells_are_neutralized_for_formula_leading_field_names():
    poisoned = [
        {
            "entity_id": "e1",
            "entity_key": "alpha",
            "fields": {
                "=SUM(A1:A9)": {
                    "value": "x",
                    "conflict": False,
                    "missing": False,
                    "candidates": [{"source_url": "https://example.org/a"}],
                },
            },
        }
    ]
    rows = list(csv.reader(io.StringIO(render_csv(poisoned))))
    header, body = rows[0], rows[1]
    assert header == [
        "entity_id",
        "entity_key",
        "'=SUM(A1:A9)",
        "'=SUM(A1:A9)__status",
        "'=SUM(A1:A9)__sources",
    ]
    # Neutralizing the displayed header must not break the underlying field lookup:
    # the row's data still lines up under that (visually prefixed) column.
    assert body == ["e1", "alpha", "x", "ok", "https://example.org/a"]


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_header_cells_neutralized_for_every_unsafe_leading_character(prefix):
    field_name = f"{prefix}field"
    poisoned = [
        {
            "entity_id": "e1",
            "entity_key": "alpha",
            "fields": {
                field_name: {
                    "value": None,
                    "conflict": False,
                    "missing": True,
                    "candidates": [],
                },
            },
        }
    ]
    header = next(csv.reader(io.StringIO(render_csv(poisoned))))
    assert header[2] == f"'{field_name}"


def test_backfilled_missing_field_renders_as_missing():
    backfilled = [
        {
            "entity_id": "e1",
            "entity_key": "alpha",
            "fields": {
                "name": {
                    "value": "Alpha",
                    "conflict": False,
                    "missing": False,
                    "candidates": [{"source_url": "https://example.org/a"}],
                },
                "phantom_field": {
                    "value": None,
                    "conflict": False,
                    "missing": True,
                    "candidates": [],
                },
            },
        }
    ]
    rows = list(csv.DictReader(io.StringIO(render_csv(backfilled))))
    assert rows[0]["phantom_field"] == ""
    assert rows[0]["phantom_field__status"] == "missing"
    assert rows[0]["phantom_field__sources"] == ""
