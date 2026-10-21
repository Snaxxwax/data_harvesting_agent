# Starter historical breach coverage

This is the initial metadata inventory accompanying the catalog implementation. The
[UpGuard article](https://www.upguard.com/blog/biggest-data-breaches) motivated the coverage
inventory; the four rows below were checked against HIBP's public metadata endpoints.
This is not a complete mapping of UpGuard's list.

| Catalog ID | Kind | Provider metadata source | Candidate identifier lookup | Evidence detail | Searchable locally after loading starter files |
|---|---|---|---|---|---|
| `adobe-2013` | Incident | [HIBP Adobe](https://haveibeenpwned.com/api/v3/breach/Adobe) | Email | Membership; breach-wide field categories | No |
| `linkedin-2012` | Incident | [HIBP LinkedIn](https://haveibeenpwned.com/api/v3/breach/LinkedIn) | Email | Membership; breach-wide field categories | No |
| `verifications-io-2019` | Exposure | [HIBP Verifications.io](https://haveibeenpwned.com/api/v3/breach/VerificationsIO) | Email | Membership; breach-wide field categories | No |
| `collection-1` | Compilation | [HIBP Collection #1](https://haveibeenpwned.com/api/v3/breach/Collection1) | Email | Membership; breach-wide field categories | No |

The machine-readable files preserve provider-reported counts with their units, field
categories, and reference check timestamps. HIBP marks Collection #1 unverified. Its
provider-assigned date is not stored as a single breach date. LinkedIn 2012 remains
distinct from the later scraping incident.

HIBP account searches require an eligible subscription. Public catalog presence does not
prove a particular account will be returned, establish complete coverage, or provide that
person's underlying record. See [HIBP's API documentation](https://haveibeenpwned.com/API/v3).
No customer identifiers were queried to assemble this starter; no paid runs were made.

## Expanding coverage

1. Add an incident with references, distinguishing primary reporting from provider metadata.
2. Record each provider's supported identifiers, evidence detail, and last check.
3. Import authorized normalized records with the explicit catalog `breach_id`.
4. Query the live matrix; it derives local availability from indexed records.
5. Preserve unsupported and unavailable incidents so empty search results cannot imply
   they were checked.

Provider connectors, full-list mapping, and automated metadata refresh remain future work.
