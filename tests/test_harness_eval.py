"""The evaluation's metrics must not report success they did not measure."""

import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "harness_eval", Path(__file__).parent.parent / "scripts" / "harness_eval.py"
)
he = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(he)

BODY = json.dumps({"results": [{"url": "https://github.com/Snaxxwax", "username": "Snaxxwax"}]})


def _obs(**kw):
    return {"capture_ids": [1], "locator": "/results/0/url", "method": "structured", **kw}


def test_support_checks_the_value_at_the_locator_not_just_a_citation():
    body = {1: BODY}.__getitem__
    assert he.support(_obs(value='"https://github.com/Snaxxwax"'), body) == "supported"
    # Cited, but the capture says something else there: present citation, no support.
    assert he.support(_obs(value='"https://evil.test/x"'), body) == "unsupported"
    assert he.support(_obs(value='"x"', locator="/results/9/url"), body) == "unsupported"
    assert he.support(_obs(value='"x"', capture_ids=[]), body) == "uncited"
    assert he.support(_obs(value='"candidate"', method="derived:attribution/2"), body) == "derived"
    # Non-JSON capture: verbatim presence in the body.
    assert (
        he.support(_obs(value='"Alpha"', locator="text:0"), {1: "<p>Alpha</p>"}.__getitem__)
        == "supported"
    )


def test_attribution_without_labels_is_not_evaluated_not_zero():
    assert he.score_attribution({"https://x/a"}, None)["status"] == "not_evaluated"


def test_attribution_counts_incorrect_and_unsupported_associations():
    labels = {"owned": ["https://github.com/Snaxxwax"], "not_owned": ["https://twitch.tv/snaxxwax"]}
    s = he.score_attribution(
        {"https://github.com/Snaxxwax", "https://twitch.tv/snaxxwax", "https://x.test/s"}, labels
    )
    assert s["correct"] == 1
    assert s["incorrect"] == ["https://twitch.tv/snaxxwax"]
    assert s["unsupported"] == ["https://x.test/s"]
    assert s["errors"] == 2
    assert he.score_attribution(set(), labels)["missed_owned"] == ["https://github.com/Snaxxwax"]


def test_support_decodes_entities_and_typed_numbers_from_page_extractions():
    page = {1: "<title>a &middot; b</title><b>566290833</b>"}.__getitem__
    assert he.support(_obs(value='"a · b"', locator="title"), page) == "supported"
    assert he.support(_obs(value="566290833", locator="socid:x"), page) == "supported"
    assert he.support(_obs(value="42", locator="socid:x"), page) == "unsupported"
