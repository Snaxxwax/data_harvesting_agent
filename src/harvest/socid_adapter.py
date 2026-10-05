"""Extract profile assertions from captured HTML without acquiring another response."""

from __future__ import annotations

import math
import re

import socid_extractor

from .extract import HtmlAdapter
from .models import Claim, canonical_url


def _warn(result, message: str):
    if message not in result.warnings and len(result.warnings) < 50:
        result.warnings.append(message)


def _normalized(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _structural_span(page: str, field: str, value: str):
    """(start, end) of `value` where markup assigns it to a key naming `field`
    (`"followerCount":22` for follower_count, `uid:1`/`data-uid='1'` for mal_uid): the first
    such occurrence, since every one of them sits under the field's own key (TikTok repeats
    its stats); None when none does. A site-prefixed field may match its bare key
    (mal_uid ~ uid); a mere suffix (username ~ name) may not."""
    names = {_normalized(field)}
    if "_" in field:
        names.add(_normalized(field.split("_", 1)[1]))
    pattern = re.compile(
        rf"""(?<![A-Za-z0-9_])["']?([A-Za-z0-9_]+)["']?\s*[:=]\s*["']?({re.escape(value)})(?![\w])"""
    )
    hits = [m.span(2) for m in pattern.finditer(page) if _normalized(m.group(1)) in names]
    return hits[0] if hits else None


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
            locator = f"socid:{scheme}:chars:{start}-{start + len(evidence)}"
            if page.find(evidence, start + 1) >= 0:
                # The value occurs more than once ("22", "0", a name in title and body), so a
                # literal match does not show which occurrence supports this field. Use the
                # structural occurrence -- the value under this field's own key in embedded
                # data -- or drop the claim; a lowered confidence is not field-level proof.
                span = _structural_span(page, field, evidence)
                if span is None:
                    _warn(
                        result, "socid value omitted: ambiguous on the page, no structural locator"
                    )
                    continue
                locator = f"socid:{scheme}:field:{span[0]}-{span[1]}"
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
