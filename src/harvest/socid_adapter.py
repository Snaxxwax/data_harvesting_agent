"""Extract profile assertions from captured HTML without acquiring another response."""

from __future__ import annotations

import math

import socid_extractor

from .extract import HtmlAdapter
from .models import Claim, canonical_url


def _warn(result, message: str):
    if message not in result.warnings and len(result.warnings) < 50:
        result.warnings.append(message)


class SocidHtmlAdapter(HtmlAdapter):
    name = "socid-html/1"

    def extract(self, body: bytes, url: str):
        result = super().extract(body, url)
        # Keep ordinary HTML revisions and model-input audit labels stable when
        # this library finds nothing usable on the page.
        result.extractor = HtmlAdapter.name
        page = body.decode("utf-8", errors="replace")
        try:
            fields = socid_extractor.extract(page)
        except Exception as exc:
            _warn(result, f"socid extraction failed: {type(exc).__name__}")
            return result
        if not isinstance(fields, dict):
            _warn(result, "socid extraction returned a non-object")
            return result

        scheme = str(fields.get("_extractor", "unknown"))[:100]
        page_key = "url:" + canonical_url(url)
        for field, value in fields.items():
            if field == "_extractor" or value is None or value == "":
                continue
            if len(result.claims) >= 5000:
                _warn(result, "socid claim limit reached")
                break
            if not isinstance(field, str) or not 0 < len(field) <= 200:
                _warn(result, "socid field name omitted")
                continue
            if not isinstance(value, (str, int, float, bool)) or (
                isinstance(value, float) and not math.isfinite(value)
            ):
                continue
            evidence = str(value)
            if len(evidence) > 20000:
                _warn(result, "socid oversize field omitted")
                continue
            start = page.find(evidence)
            if start < 0:
                _warn(result, "socid value without literal source evidence omitted")
                continue
            # A short ID may occur in unrelated markup before the parser's actual
            # source. Without an offset from socid, do not pretend the first match
            # identifies the supporting occurrence.
            locator = (
                f"socid:{scheme}:chars:{start}-{start + len(evidence)}"
                if page.find(evidence, start + 1) < 0
                else f"socid:{scheme}:ambiguous-value:{field}"
            )
            result.claims.append(
                Claim(
                    entity_key=page_key,
                    field=field,
                    value=value,
                    evidence=evidence,
                    locator=locator,
                    method="html",
                )
            )
            result.extractor = self.name
        return result
