"""Provider attribution for SearXNG responses; HTTP success is not provider health."""

from urllib.parse import urlsplit


def provider_outcomes(payload, results, accepted):
    rows = {}

    def row(name):
        return rows.setdefault(
            str(name),
            {
                "engine": str(name),
                "outcome": "completed",
                "reason": None,
                "accepted": set(),
                "rejected": set(),
            },
        )

    for result in results:
        names = result.get("engines") or [result.get("engine") or "unattributed"]
        if isinstance(names, str):
            names = [names]
        url = result.get("url", "")
        for name in names:
            row(name)["accepted" if url in accepted else "rejected"].add(url)
    for failure in payload.get("unresponsive_engines") or []:
        if not isinstance(failure, (list, tuple)) or len(failure) < 2:
            continue
        name, reason = failure[:2]
        reason = str(reason)
        item = row(name)
        item["outcome"] = (
            "blocked"
            if any(
                term in reason.lower()
                for term in ("captcha", "rate", "too many", "suspend", "forbidden", "unauthorized")
            )
            else "errored"
        )
        item["reason"] = reason
    return [
        {**item, "accepted": len(item["accepted"]), "rejected": len(item["rejected"])}
        for item in rows.values()
    ]


def known_answer(payload):
    """One stable public answer; retain per-engine attribution and failures."""
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise ValueError("search results contract mismatch")
    results = [r for r in results if isinstance(r, dict)]
    relevant = {
        str(r.get("url", ""))
        for r in results
        if urlsplit(str(r.get("url", ""))).hostname in {"iana.org", "www.iana.org"}
        and "domain" in (str(r.get("title", "")) + " " + str(r.get("content", ""))).lower()
    }
    providers = provider_outcomes(payload, results, relevant)
    for provider in providers:
        if provider["outcome"] == "completed" and not provider["accepted"]:
            provider.update(
                outcome="errored", reason="known-answer query returned no relevant source"
            )
    return {
        "ready": bool(relevant),
        "relevant": len(relevant),
        "returned": len(results),
        "providers": providers,
    }
