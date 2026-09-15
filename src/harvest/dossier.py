"""Deterministic, job-scoped read projection reconciling source-local entities to declared
investigation targets. Exact identifier matching only: no fuzzy merge, no vote, no score.

`reconcile()` operates purely on already-fetched job-scoped observation rows (the shape
returned by `Store.observations(job_id, ...)`); it performs no I/O so the matching/conflict
logic is directly unit-testable without a database.
"""

from __future__ import annotations

from .store import packed


def _identifier_projection(observations, identifier_fields):
    """namespace -> set of exact string values this entity declared under that namespace.

    Non-string values (e.g. a JSON number or boolean) are never projected: declared target
    identifiers are always strings, so a differently-typed source value can never match one.
    This preserves value type instead of coercing, and preserves leading zeroes/case because
    the string is used exactly as observed.
    """
    projection: dict[str, set[str]] = {}
    for obs in observations:
        namespace = identifier_fields.get(obs["field"])
        if namespace is None:
            continue
        value = obs["value"]
        if not isinstance(value, str) or not value:
            continue
        projection.setdefault(namespace, set()).add(value)
    return projection


def _match_targets(projection, targets):
    """A target is a candidate if any declared namespace/value pair overlaps its identifiers.

    A candidate target is contradicted if some OTHER namespace it also declares is present on
    the entity but does not overlap. Only entities that are candidates can be contradicted;
    a target the entity never matched is simply not relevant, not ambiguous.
    """
    candidates: set[str] = set()
    contradicted: set[str] = set()
    matches: list[tuple[str, str, str]] = []
    for target in targets.values():
        matched_here = False
        contradicted_here = False
        for namespace, values in projection.items():
            declared = target.identifiers.get(namespace)
            if declared is None:
                continue
            overlap = values & set(declared)
            if overlap:
                matched_here = True
                matches.extend((target.key, namespace, value) for value in sorted(overlap))
            elif values:
                contradicted_here = True
        if matched_here:
            candidates.add(target.key)
        if contradicted_here:
            contradicted.add(target.key)
    return candidates, contradicted, matches


def _candidate(obs, basis, match):
    return {
        "source_field": obs["field"],
        "value": obs["value"],
        "entity_id": obs["entity_id"],
        "entity_key": obs["entity_key"],
        "observation_id": obs["id"],
        "source_url": obs["source_url"],
        "capture_ids": obs["capture_ids"],
        "extraction_ids": obs["extraction_ids"],
        "retrieved": obs["last_seen"],
        "evidence": obs["evidence"],
        "locator": obs["locator"],
        "method": obs["method"],
        "extraction_confidence": obs["confidence"],
        "basis": basis,
        "match": match,
    }


def _declared_dossier_fields(investigation, target_key):
    """Dossier fields any admitted record could plausibly supply for this target.

    Identifier-matched rules apply to whichever target their records resolve to, so their
    field_map outputs are expected for every target. Document-binding rules are explicit and
    per-target, so only their whitelisted fields are expected for their declared target.
    """
    fields: set[str] = set()
    for rule in investigation.sources:
        if rule.identifier_fields:
            fields.update(rule.field_map.values())
        if rule.document_target == target_key:
            fields.update(rule.field_map.get(f, f) for f in rule.document_fields)
    return fields


def reconcile(investigation, observations):
    """Build the target/unresolved/ambiguous view of a job's own scoped observations.

    `observations` must already be scoped to one job (e.g. `Store.observations(job_id, ...)`
    exhausted across pages) -- this function trusts that scoping and adds no job filtering of
    its own, since the caller's job-scoped query is what keeps one job's dossier from ever
    being changed by another job's assertions.
    """
    targets = {t.key: t for t in investigation.targets}
    rules = {s.url: s for s in investigation.sources}

    entities: dict[tuple[str, str], dict] = {}
    for obs in observations:
        rule = rules.get(obs["source_url"])
        if rule is None:
            continue
        key = (obs["source_url"], obs["entity_id"])
        entry = entities.setdefault(
            key,
            {
                "entity_key": obs["entity_key"],
                "source_url": obs["source_url"],
                "rule": rule,
                "observations": [],
            },
        )
        entry["observations"].append(obs)

    target_fields: dict[str, dict[str, list]] = {key: {} for key in targets}
    matched_entities: dict[str, list] = {key: [] for key in targets}
    unresolved = []
    ambiguous = []

    def add_candidate(target_key, dossier_field, obs, basis, match):
        target_fields[target_key].setdefault(dossier_field, []).append(
            _candidate(obs, basis, match)
        )

    for (source_url, entity_id), entry in entities.items():
        rule = entry["rule"]
        obs_list = entry["observations"]
        is_document = entry["entity_key"] == "url:" + rule.url

        if (rule.identifier_fields or rule.field_map) and not (
            is_document and rule.document_target
        ):
            projection = _identifier_projection(obs_list, rule.identifier_fields)
            candidates, contradicted, matches = _match_targets(projection, targets)
            viable = candidates - contradicted
            if len(viable) == 1:
                target_key = next(iter(viable))
                match = next((m for m in matches if m[0] == target_key), None)
                match_info = {"namespace": match[1], "value": match[2]} if match else None
                matched_entities[target_key].append(
                    {
                        "entity_id": entity_id,
                        "entity_key": entry["entity_key"],
                        "source_url": source_url,
                        "basis": "identifier_match",
                        "match": match_info,
                    }
                )
                for obs in obs_list:
                    dossier_field = rule.field_map.get(obs["field"])
                    if dossier_field:
                        add_candidate(
                            target_key, dossier_field, obs, "identifier_match", match_info
                        )
            elif candidates:
                ambiguous.append(
                    {
                        "source_url": source_url,
                        "entity_id": entity_id,
                        "entity_key": entry["entity_key"],
                        "reason": "competing_target_matches"
                        if len(viable) > 1
                        else "contradicted_identifier",
                        "candidate_targets": sorted(candidates),
                        "contradicted_targets": sorted(contradicted),
                        "observation_ids": [o["id"] for o in obs_list],
                    }
                )
            else:
                unresolved.append(
                    {
                        "source_url": source_url,
                        "entity_id": entity_id,
                        "entity_key": entry["entity_key"],
                        "reason": "no_matching_identifier",
                        "observation_ids": [o["id"] for o in obs_list],
                    }
                )

        if is_document and rule.document_target:
            target_key = rule.document_target
            matched_entities[target_key].append(
                {
                    "entity_id": entity_id,
                    "entity_key": entry["entity_key"],
                    "source_url": source_url,
                    "basis": "document_binding",
                    "match": None,
                }
            )
            for obs in obs_list:
                if obs["field"] in rule.document_fields:
                    dossier_field = rule.field_map.get(obs["field"], obs["field"])
                    add_candidate(target_key, dossier_field, obs, "document_binding", None)

    targets_out = []
    for key, target in targets.items():
        fields = {}
        for dossier_field, candidates in target_fields[key].items():
            unique = {packed(c["value"]) for c in candidates}
            fields[dossier_field] = {
                "value": candidates[0]["value"] if len(unique) == 1 else None,
                "conflict": len(unique) > 1,
                "candidates": candidates,
                "supporting_source_urls": sorted({c["source_url"] for c in candidates}),
            }
        missing = sorted(f for f in _declared_dossier_fields(investigation, key) if f not in fields)
        targets_out.append(
            {
                "key": target.key,
                "label": target.label,
                "identifiers": target.identifiers,
                "matched_entities": matched_entities[key],
                "fields": fields,
                "missing_fields": missing,
            }
        )

    return {"targets": targets_out, "unresolved": unresolved, "ambiguous": ambiguous}


def _escape(value) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(dossier: dict) -> str:
    """Readable, evidence-linked text. A pure function of the JSON dossier -- no I/O."""
    lines = [f"# Dossier for job `{dossier['job_id']}` (dataset `{dossier['dataset']}`)", ""]
    for target in dossier["targets"]:
        lines.append(f"## Target: {target['label']} (`{target['key']}`)")
        lines.append("")
        if target["identifiers"]:
            lines.append("Declared identifiers:")
            for namespace, values in target["identifiers"].items():
                lines.append(f"- `{namespace}`: {', '.join(_escape(v) for v in values)}")
            lines.append("")
        lines.append(f"Matched source entities: {len(target['matched_entities'])}")
        for entity in target["matched_entities"]:
            basis = entity["basis"]
            if basis == "document_binding":
                note = "document binding (operator assertion, not evidence of identity)"
            else:
                match = entity["match"] or {}
                note = f"identifier match {match.get('namespace')}={match.get('value')}"
            lines.append(
                f"- `{_escape(entity['entity_key'])}` from "
                f"[{_escape(entity['source_url'])}]({entity['source_url']}) — {note}"
            )
        lines.append("")
        lines.append("### Fields")
        lines.append("")
        if not target["fields"]:
            lines.append("_No fields reconciled for this target._")
        for field, info in sorted(target["fields"].items()):
            status = "CONFLICT (no chosen value)" if info["conflict"] else _escape(info["value"])
            lines.append(f"**{field}**: {status}")
            for candidate in info["candidates"]:
                lines.append(
                    f"  - `{_escape(candidate['value'])}` via "
                    f"[{_escape(candidate['source_url'])}]({candidate['source_url']}) "
                    f"(source field `{candidate['source_field']}`, basis {candidate['basis']}, "
                    f"method {candidate['method']}, confidence {candidate['extraction_confidence']}, "
                    f"observation {candidate['observation_id']}, "
                    f"capture {candidate['capture_ids']}, extraction {candidate['extraction_ids']}, "
                    f'retrieved {candidate["retrieved"]}) — evidence: "{_escape(candidate["evidence"])}" '
                    f"at {_escape(candidate['locator'])}"
                )
        lines.append("")
        if target["missing_fields"]:
            lines.append("Missing fields: " + ", ".join(target["missing_fields"]))
        lines.append("")
    if dossier["unresolved"]:
        lines.append("## Unresolved records")
        lines.append("")
        for record in dossier["unresolved"]:
            lines.append(
                f"- `{_escape(record['entity_key'])}` from "
                f"[{_escape(record['source_url'])}]({record['source_url']}) — {record['reason']} "
                f"(observations {record['observation_ids']})"
            )
        lines.append("")
    if dossier["ambiguous"]:
        lines.append("## Ambiguous records (excluded, not admitted to any target)")
        lines.append("")
        for record in dossier["ambiguous"]:
            lines.append(
                f"- `{_escape(record['entity_key'])}` from "
                f"[{_escape(record['source_url'])}]({record['source_url']}) — {record['reason']} "
                f"(candidates: {record['candidate_targets']}, "
                f"contradicted: {record['contradicted_targets']}, "
                f"observations {record['observation_ids']})"
            )
        lines.append("")
    lines.append("## Declared sources")
    lines.append("")
    lines.append("| URL | Acquired | HTTP status | Extraction state | Warnings |")
    lines.append("|---|---|---|---|---|")
    for source in dossier["sources"]:
        lines.append(
            f"| [{_escape(source['url'])}]({source['url']}) | {source['acquired']} | "
            f"{source['http_status']} | {source['extraction_state']} | {source['warnings']} |"
        )
    return "\n".join(lines) + "\n"
