"""Reproducible synthetic input-coverage probe; no network or model-quality claim."""

import json
import time

from harvest.passages import select_passages


def document(insertions, length=95000):
    text = ("Routine audit methodology and appendix. " * 3000)[:length]
    for offset, quote in sorted(insertions, reverse=True):
        text = text[:offset] + quote + text[offset + len(quote) :]
    return text


def cases():
    quote = "Northstar accreditation is ISO 27001."
    for offset in (500, 8500, 14000, 25100, 37300, 61000, 89500):
        yield f"fact-at-{offset}", document([(offset, quote)]), ["accreditation"], [quote]
    revoked = "Northstar accreditation was revoked in 2026."
    yield (
        "distant-conflict",
        document([(27000, quote), (78000, revoked)]),
        ["accreditation"],
        [quote, revoked],
    )
    synonym = "The certificate is registered under ISO 27001."
    yield "unmatched-vocabulary", document([(33100, synonym)]), ["accreditation"], [synonym]
    fields = ["accreditation", "employees", "jurisdiction", "revenue", "founded", "ownership"]
    facts = [f"Northstar {field} has recorded value {i + 42}." for i, field in enumerate(fields)]
    yield (
        "six-competing-fields",
        document(list(zip(range(17000, 89000, 12000), facts, strict=True))),
        fields,
        facts,
    )


def main():
    results = []
    started = time.perf_counter()
    for name, text, fields, facts in cases():
        selection = select_passages(text, "Investigate Northstar", fields, {})
        # Equal-sized character windows at five evenly spaced positions, with no ranking.
        uniform = [
            text[round((len(text) - 2600) * f) : round((len(text) - 2600) * f) + 2600]
            for f in (0, 0.25, 0.5, 0.75, 1)
        ]
        results.append(
            {
                "case": name,
                "facts": len(facts),
                "prefix_hits": sum(f in text[:14000] for f in facts),
                "uniform_hits": sum(any(f in part for part in uniform) for f in facts),
                "ranked_hits": sum(selection.locate(f) is not None for f in facts),
                "selected_chars": selection.metadata["selected_chars"],
                "input_chars": len(selection.text),
            }
        )
    print(
        json.dumps(
            {
                "kind": "synthetic exact-quote input coverage; no model calls",
                "elapsed_seconds": round(time.perf_counter() - started, 4),
                "totals": {
                    key: sum(r[key] for r in results)
                    for key in ("facts", "prefix_hits", "uniform_hits", "ranked_hits")
                },
                "cases": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
