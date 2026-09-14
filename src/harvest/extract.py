from __future__ import annotations

import csv
import io
import itertools
import json
from importlib.metadata import entry_points
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Claim, Extraction, ExtractionBatch, Lead, canonical_url
from .store import digest, packed


class Adapter(Protocol):
    """Trusted installed code only. Plugins never perform network IO."""

    name: str

    def accepts(self, content_type: str, url: str) -> bool: ...
    def extract(self, body: bytes, url: str) -> Extraction: ...


def http_url(value, base=None):
    if not isinstance(value, str):
        return None
    try:
        return canonical_url(urljoin(base, value) if base else value)
    except ValueError:
        return None


def record_key(record, url, index):
    for field in ("@id", "url", "html_url"):
        value = record.get(field)
        if isinstance(value, str):
            absolute = urljoin(url, value) if field == "@id" else value
            if candidate := http_url(absolute):
                fragment = urlsplit(absolute).fragment
                return "url:" + candidate + ("#" + fragment if fragment else "")
    p = urlsplit(url)
    # A source-local ID is not globally unique. Include origin AND collection path.
    for field in ("id", "identifier", "uuid"):
        value = record.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return f"source:{p.scheme}://{p.netloc}{p.path}:{field}:{value}"
    # Missing identity: exact record fingerprint, never a guessed fuzzy merge.
    return f"record:{url}:{digest(packed(record))}"


def records_extraction(records, url, prefix="", extractor="json/1", single=False, offset=0):
    claims, leads, warnings = [], [], []
    if len(records) > 100:
        warnings.append(
            "record limit: only the first 100 records were extracted; raw evidence retained"
        )
    for index, record in enumerate(records[:100], start=offset):
        if not isinstance(record, dict):
            if "non-object records skipped" not in warnings:
                warnings.append("non-object records skipped")
            continue
        key = record_key(record, url, index)
        if len(record) > 100 and "field limit: first 100 fields per record" not in warnings:
            warnings.append("field limit: first 100 fields per record")
        for field, value in list(record.items())[:100]:
            if len(claims) >= 5000:
                if "response limit: first 5000 observations" not in warnings:
                    warnings.append("response limit: first 5000 observations")
                break
            if value is None or field == "@context":
                continue
            evidence = packed(value)
            if len(evidence) > 20000:
                if "oversize field values omitted; raw evidence retained" not in warnings:
                    warnings.append("oversize field values omitted; raw evidence retained")
                continue
            record_path = prefix if single else f"{prefix}/{index}"
            claims.append(
                Claim(
                    entity_key=key,
                    field=str(field)[:200],
                    value=value,
                    evidence=evidence,
                    locator=f"{record_path}/{str(field).replace('~', '~0').replace('/', '~1')}",
                    method="structured",
                    confidence=1,
                )
            )
            # Relationships and identifiers become leads, never implicit cross-source merges.
            values = value if isinstance(value, list) else [value]
            for candidate in values[:20]:
                if isinstance(candidate, dict):
                    candidate = candidate.get("@id") or candidate.get("url")
                link = http_url(candidate)
                if link:
                    leads.append(
                        Lead(url=link, reason=f"identifier or relationship: {field}", priority=20)
                    )
    return Extraction(
        claims=claims,
        leads=leads[:500],
        text=packed(records)[:100000],
        extractor=extractor,
        warnings=warnings,
    )


def record_batches(
    records, url, *, start, maximum, extractor, prefix="", single=False, total=None, leads=()
):
    """One parser pass per lease attempt. Durable record ordinal is the restart cursor."""
    iterator = iter(itertools.islice(records, start, None))
    sentinel = object()
    pending = next(iterator, sentinel)
    cursor, remaining = start, maximum
    if pending is sentinel:
        yield ExtractionBatch(
            extraction=Extraction(extractor=extractor, leads=list(leads)),
            start=start,
            end=start,
            total=start,
            done=True,
        )
        return
    while remaining > 0:
        batch = [pending]
        batch.extend(itertools.islice(iterator, min(50, remaining) - 1))
        pending = next(iterator, sentinel)
        done = pending is sentinel
        result = records_extraction(batch, url, prefix, extractor, single, offset=cursor)
        result.leads = list(leads) + result.leads if cursor == start else result.leads
        result.leads = result.leads[:500]
        end = cursor + len(batch)
        yield ExtractionBatch(
            extraction=result, start=cursor, end=end, total=end if done else total, done=done
        )
        cursor, remaining = end, remaining - len(batch)
        if done:
            return


class JsonAdapter:
    name = "json/1"

    def accepts(self, content_type, url):
        return "json" in content_type or urlsplit(url).path.endswith(".json")

    def records(self, body):
        def reject_constant(value):
            raise ValueError(f"invalid JSON number {value}")

        data = json.loads(body, parse_constant=reject_constant)
        prefix = ""
        records = data
        if isinstance(data, dict):
            for field in ("@graph", "results", "items", "records", "data"):
                if isinstance(data.get(field), list):
                    records, prefix = data[field], "/" + field
                    break
            else:
                records = [data]
        if not isinstance(records, list):
            raise ValueError("JSON source must contain an object or list of objects")
        single = isinstance(data, dict) and not prefix
        return data, records, prefix, single

    def pagination(self, data, url):
        if isinstance(data, dict):
            next_link = data.get("next")
            if isinstance(data.get("links"), dict):
                next_link = data["links"].get("next", next_link)
            if next_link and (link := http_url(next_link, url)):
                return [Lead(url=link, reason="pagination", priority=50)]
        return []

    def extract(self, body, url):
        data, records, prefix, single = self.records(body)
        result = records_extraction(records, url, prefix, self.name, single=single)
        result.leads = self.pagination(data, url) + result.leads
        return result

    def batches(self, body, url, start, maximum):
        data, records, prefix, single = self.records(body)
        return record_batches(
            records,
            url,
            start=start,
            maximum=maximum,
            extractor=self.name,
            prefix=prefix,
            single=single,
            total=len(records),
            leads=self.pagination(data, url),
        )


class CsvAdapter:
    name = "csv/1"

    def accepts(self, content_type, url):
        return "csv" in content_type or urlsplit(url).path.endswith(".csv")

    def extract(self, body, url):
        import itertools

        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
        records = list(itertools.islice(reader, 101))
        if any(None in r for r in records):
            raise ValueError("CSV rows have more values than headers")
        return records_extraction(records, url, "row", self.name)

    def batches(self, body, url, start, maximum):
        reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig"), newline=""), strict=True)
        headers = reader.fieldnames
        if not headers or any(not h for h in headers) or len(set(headers)) != len(headers):
            raise ValueError("CSV requires unique non-empty headers")

        def records():
            for row in reader:
                if None in row or any(v is None for v in row.values()):
                    raise ValueError("CSV row width does not match headers")
                yield row

        return record_batches(
            records(), url, start=start, maximum=maximum, extractor=self.name, prefix="row"
        )


class HtmlAdapter:
    name = "html-jsonld/1"

    def accepts(self, content_type, url):
        return "html" in content_type

    def extract(self, body, url):
        soup = BeautifulSoup(body, "html.parser")
        claims, leads, warnings = [], [], []
        for index, script in enumerate(soup.select('script[type="application/ld+json"]')[:20]):
            try:
                data = JsonAdapter().extract(script.get_text().encode(), url)
            except (ValueError, TypeError, RecursionError):
                warnings.append("invalid JSON-LD block omitted")
                continue
            for claim in data.claims:
                claim.locator = f"script:application/ld+json:{index}" + claim.locator
            claims.extend(data.claims)
            leads.extend(data.leads)
            warnings.extend(data.warnings)
        page_key = "url:" + canonical_url(url)
        if soup.title and soup.title.get_text(strip=True):
            title = soup.title.get_text(" ", strip=True)[:2000]
            claims.append(
                Claim(
                    entity_key=page_key,
                    field="page_title",
                    value=title,
                    evidence=title,
                    locator="title",
                    method="html",
                )
            )
        description = soup.find("meta", attrs={"name": "description"})
        if description and description.get("content"):
            value = str(description["content"])[:5000]
            claims.append(
                Claim(
                    entity_key=page_key,
                    field="page_description",
                    value=value,
                    evidence=value,
                    locator="meta[name=description]",
                    method="html",
                )
            )
        for node in soup.select("a[href], link[rel=next]")[:500]:
            link = http_url(node.get("href"), url)
            if link and link != url:
                text = node.get_text(" ", strip=True)[:200]
                rel = node.get("rel", [])
                pagination = "next" in rel
                reason = "pagination" if pagination else "link: " + text
                leads.append(Lead(url=link, reason=reason, priority=50 if pagination else 0))
        for node in soup(["script", "style", "noscript", "nav", "footer"]):
            node.decompose()
        text = soup.get_text(" ", strip=True)
        if len(text) > 100000:
            warnings.append("normalized text exceeds 100000 characters; remainder omitted")
        return Extraction(
            claims=claims[:5000],
            leads=leads[:500],
            text=text[:100000],
            extractor=self.name,
            warnings=list(dict.fromkeys(warnings))[:50],
        )


class TextAdapter:
    name = "text/1"

    def accepts(self, content_type, url):
        return content_type.startswith("text/plain")

    def extract(self, body, url):
        text = body.decode("utf-8", errors="replace")
        return Extraction(
            text=text[:100000],
            extractor=self.name,
            warnings=["normalized text exceeds 100000 characters; remainder omitted"]
            if len(text) > 100000
            else [],
        )


class Extractors:
    def __init__(self):
        self.adapters = [ep.load()() for ep in entry_points(group="harvest.adapters")]
        self.adapters += [JsonAdapter(), CsvAdapter(), HtmlAdapter(), TextAdapter()]

    def extract(self, body: bytes, url: str, content_type: str):
        for adapter in self.adapters:
            if adapter.accepts(content_type.lower(), url):
                result = adapter.extract(body, url)
                return Extraction.model_validate(result.model_dump())
        raise ValueError(f"unsupported content type: {content_type[:100]}")

    def batches(self, body, url, content_type, start, maximum):
        for adapter in self.adapters:
            if adapter.accepts(content_type.lower(), url):
                # Existing third-party subclasses may override extract only. Do not bypass them.
                if type(adapter) in (JsonAdapter, CsvAdapter):
                    return adapter.batches(body, url, start, maximum)
                return None
        return None
