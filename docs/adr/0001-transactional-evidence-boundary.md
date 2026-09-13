# ADR 0001: one transactional evidence boundary

Status: accepted for the first single-host milestone; revisit after measured workload limits.

The essential failure case is a worker dying between acquiring evidence, storing assertions,
adding leads and acknowledging work. An independently committed crawler queue requires
another recovery protocol around those writes. Inspected Huey/Crawlee source made that
boundary visible; Scrapy documentation also warns about abrupt `JOBDIR` shutdown.

Use a domain frontier in the same SQLite transaction as observations, captures and events.
Issue leases and fence commits with random tokens. Keep bounded bodies in SQLite initially.
Reuse existing HTTP, parser, validation and database implementations; do not implement a
generic broker or a database. Use rollback journaling with FULL sync for portable first-run
correctness, accepting serialized writes and no multi-host topology.

Consequences: a small deployment and coherent backup; at-least-once acquisition, conservative
reserved cost after failures; limited write throughput and storage efficiency. PostgreSQL
and object storage require workload evidence and a tested migration, not a silent backend
swap. Process-kill and transaction-failure tests support the current choice, not unlimited scale.
