import pytest

from harvest.planning import (
    DEFAULT_FIELDS,
    detect_investigation_type,
    plan_dataset,
    plan_investigation,
)


@pytest.mark.parametrize(
    "value,kind",
    [
        ("https://example.org/path", "url"),
        ("HTTP://Example.org", "url"),
        ("example.org", "domain"),
        ("sub.example.co.uk", "domain"),
        ("jane.doe@example.org", "email"),
        ("+1 (415) 555-0100", "phone"),
        ("4155550100", "phone"),
        ("@janedoe", "username"),
    ],
)
def test_detect_obvious_types(value, kind):
    assert detect_investigation_type(value) == kind


@pytest.mark.parametrize(
    "value", ["Jane Doe", "Acme Corp", "123 Main St, Springfield", "case-9911"]
)
def test_ambiguous_free_text_is_unknown(value):
    assert detect_investigation_type(value) == "unknown"


def test_detect_requires_nonempty_value():
    with pytest.raises(ValueError):
        detect_investigation_type("   ")


def test_plan_investigation_url_seed_needs_no_search():
    plan = plan_investigation("https://example.org/profile")
    assert plan.kind == "url"
    assert plan.seeds == ["https://example.org/profile"]
    assert plan.discovery_queries == []
    assert plan.fields


def test_plan_investigation_domain_becomes_https_seed():
    plan = plan_investigation("example.org")
    assert plan.kind == "domain"
    assert plan.seeds == ["https://example.org/"]
    assert plan.discovery_queries == ["site:example.org"]


def test_plan_investigation_email_generates_bounded_deduplicated_queries():
    plan = plan_investigation("Jane.Doe@Example.org")
    assert plan.kind == "email"
    assert plan.normalized == "jane.doe@example.org"
    assert plan.seeds == []
    assert plan.discovery_queries
    assert len(plan.discovery_queries) == len(set(plan.discovery_queries))
    assert len(plan.discovery_queries) <= 5


def test_plan_investigation_unknown_type_requires_explicit_kind():
    with pytest.raises(ValueError, match="explicitly"):
        plan_investigation("Jane Doe")


def test_plan_investigation_explicit_kind_bypasses_detection():
    plan = plan_investigation("Jane Doe", kind="person")
    assert plan.kind == "person"
    assert plan.discovery_queries
    assert plan.fields == DEFAULT_FIELDS["person"]


def test_plan_investigation_explicit_kind_still_validates_shape():
    with pytest.raises(ValueError):
        plan_investigation("not a url", kind="url")


def test_plan_investigation_rejects_unknown_kind():
    with pytest.raises(ValueError):
        plan_investigation("value", kind="alien")


def test_plan_investigation_rejects_empty_value():
    with pytest.raises(ValueError):
        plan_investigation("   ")


@pytest.mark.parametrize("kind", ["organization", "address", "identifier", "username", "phone"])
def test_plan_investigation_every_type_has_default_fields(kind):
    plan = plan_investigation("some-value", kind=kind)
    assert plan.fields


def test_plan_dataset_marketplace_listing_defaults():
    plan = plan_dataset("used RTX 3090 marketplace listings")
    assert "price" in plan.fields
    assert "title" in plan.fields
    assert "url" in plan.fields
    assert plan.seeds == []
    assert plan.discovery_queries
    assert len(plan.discovery_queries) <= 5


def test_plan_dataset_homes_under_budget_defaults():
    plan = plan_dataset("homes under $350,000")
    assert {"address", "price", "bedrooms", "bathrooms"}.issubset(set(plan.fields))


def test_plan_dataset_category_products_defaults():
    plan = plan_dataset("office chair category products")
    assert {"title", "price", "brand", "url"}.issubset(set(plan.fields))


def test_plan_dataset_seed_urls_skip_discovery_queries():
    plan = plan_dataset("widgets", seeds=["https://example.org/widgets"])
    assert plan.seeds == ["https://example.org/widgets"]
    assert plan.discovery_queries == []


def test_plan_dataset_allows_editing_fields_and_seeds():
    plan = plan_dataset(
        "widgets", fields=["custom_field", "custom_field"], seeds=["https://example.org/a"]
    )
    assert plan.fields == ["custom_field"]
    assert plan.seeds == ["https://example.org/a"]


def test_plan_dataset_requires_nonempty_description():
    with pytest.raises(ValueError):
        plan_dataset("   ")


def test_plan_dataset_marketplace_defaults_include_seller_and_availability():
    plan = plan_dataset("used RTX 3090 marketplace listings")
    assert {"seller", "availability"}.issubset(set(plan.fields))


def test_plan_dataset_product_defaults_include_seller_and_availability():
    plan = plan_dataset("office chair category products")
    assert {"seller", "availability"}.issubset(set(plan.fields))


def test_plan_dataset_vehicle_listing_defaults():
    plan = plan_dataset("used sedans for sale")
    assert {"year", "make", "model", "mileage", "vin"}.issubset(set(plan.fields))


def test_plan_dataset_vehicle_rule_matches_truck_and_car_singular_and_plural():
    for description in ["pickup trucks for sale", "used car listings", "vehicle inventory"]:
        plan = plan_dataset(description)
        assert {"year", "make", "model", "mileage", "vin"}.issubset(set(plan.fields)), description


def test_plan_investigation_domain_includes_bounded_site_query_and_retains_seed():
    plan = plan_investigation("example.org")
    assert plan.seeds == ["https://example.org/"]
    assert plan.discovery_queries == ["site:example.org"]
    assert len(plan.discovery_queries) <= 5


@pytest.mark.parametrize(
    "value",
    [
        "https://example.org",
        "https://example.org/",
        "http://example.org/path",
        "example.org/path",
        "example.org/path/",
        "example.org:8080",
        "user:pass@example.org",
        "exa mple.org",
        "-example.org",
        "example-.org",
        "..example.org",
        " example.org",
        "example.org ",
        " example.org ",
        "\texample.org",
        "example.org\n",
        "\texample.org\n",
        "\nexample.org\t",
        "example.org////",
        "example.org//",
        "example.org..",
        "example.org...",
    ],
)
def test_plan_investigation_explicit_domain_kind_rejects_malformed_input(value):
    """Regression: an explicit kind="domain" used to skip structural validation entirely
    (detect_investigation_type, which owns the regex check, is only called when kind is
    None), so a scheme, path, port, credentials, surrounding/embedded whitespace, or more
    than one trailing slash/dot could reach `_normalize_domain`'s caller unchecked (or be
    silently normalized away by an eager .strip()/.rstrip() before validation ran) and
    produce a malformed seed/site: query."""
    with pytest.raises(ValueError, match="bare hostname"):
        plan_investigation(value, kind="domain")


@pytest.mark.parametrize(
    "value",
    [
        " example.org",
        "example.org ",
        " example.org ",
        "\texample.org",
        "example.org\n",
        "\texample.org\n",
        "example.org////",
        "example.org..",
    ],
)
def test_detect_investigation_type_rejects_whitespace_and_unbounded_trailing_domain_input(value):
    """Auto-detection must apply the exact same strict rule: whitespace-wrapped or
    over-punctuated input must not be classified as a clean "domain" just because
    detect_investigation_type's own unrelated `.strip()` (used for its other checks) would
    make it look like one."""
    assert detect_investigation_type(value) == "unknown"


@pytest.mark.parametrize(
    "value,normalized",
    [
        ("example.org", "example.org"),
        ("EXAMPLE.ORG", "example.org"),
        ("Sub.Example.Org", "sub.example.org"),
        ("deep.sub.example.org", "deep.sub.example.org"),
        ("example.org/", "example.org"),
        ("example.org.", "example.org"),
    ],
)
def test_plan_investigation_explicit_domain_kind_accepts_valid_case_insensitive_domains(
    value, normalized
):
    plan = plan_investigation(value, kind="domain")
    assert plan.kind == "domain"
    assert plan.normalized == normalized
    assert plan.seeds == [f"https://{normalized}/"]
    assert plan.discovery_queries == [f"site:{normalized}"]


def test_plan_investigation_whitespace_wrapped_domain_without_kind_requires_explicit_choice():
    """End-to-end: with no explicit kind, whitespace-wrapped domain-looking text can no
    longer sneak through as an auto-detected "domain" -- it falls to "unknown" and the
    caller must pick a type explicitly, same as any other ambiguous free text."""
    with pytest.raises(ValueError, match="explicitly"):
        plan_investigation(" example.org ")


def test_plan_investigation_unbounded_trailing_slash_without_kind_requires_explicit_choice():
    with pytest.raises(ValueError, match="explicitly"):
        plan_investigation("example.org////")


def test_detect_investigation_type_still_prefers_url_over_domain_for_scheme_input():
    assert detect_investigation_type("https://example.org") == "url"


def test_detect_investigation_type_leaves_path_bearing_host_unknown():
    assert detect_investigation_type("example.org/path") == "unknown"
