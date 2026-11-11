# Robin dark web tool

Replaces the dropped `apify_darkweb` tool with [Robin](https://github.com/apurvsinghgautam/robin)
(MIT), pinned at `b5d4f5f` (2026-10-02).

**Status (2026-11-11):** not implemented. Harvest was shut down on 2026-11-10; kept for reference.

## Decisions

- **AI-assisted, using Harvest's model.** Robin runs its whole pipeline: refine the query,
  search 16 onion search engines over Tor, have the model keep the on-topic results, read
  those pages, write a summary and suggest pivots. `HARVEST_MODEL_URL`/`NAME`/`KEY` are passed
  to Robin as `CUSTOM_API_BASE_URL`/`CUSTOM_API_MODEL`/`CUSTOM_API_KEY`. No model configured
  means `PolicyDenied`, not a silent search-only run.
- **A tool like the others.** `robin` joins `TOOLS`, opt-in through `HARVEST_TOOLS`, and runs as
  a subprocess through `_exec` (timeout, cancellation, process-group kill). Its output becomes
  one capture, so the results are replayable Harvest evidence.
- **Robin stays isolated.** Robin has no CLI or package metadata. The image clones the pinned
  commit into `/opt/robin` with its own venv (streamlit, langchain and so on never touch
  `/app/.venv`). A small driver, `src/harvest/robin_driver.py`, runs under Robin's interpreter,
  calls `pipeline.run_investigation(..., save=False)` and prints the investigation as JSON.
- **Tor runs in a separate container.** `deploy/ovh-vps/compose.robin.yaml` adds a `tor`
  service (Alpine plus the distro's `tor` package, SOCKS on 9050, reachable only on Harvest's
  networks). Robin hard-codes `127.0.0.1:9050`, so the driver points `scrape.TOR_PROXIES` and
  `search.TOR_PROXIES` at `HARVEST_TOR_PROXY` (e.g. `socks5h://tor:9050`). Unset means
  `PolicyDenied`.
- **Onion links are never fetched by Harvest.** Robin's scraper reads only `.onion` pages
  (clearweb is off). The engine sets `crawl=False` for `robin`, so returned links stay
  evidence rather than becoming fetch leads, which would fail or leak outside Tor.
- **Target format:** a free-text query, optionally prefixed with a Robin preset:
  `ransomware_malware | acme corp`. Presets: `threat_intel` (default), `ransomware_malware`,
  `personal_identity`, `corporate_espionage`. Planning kinds: `organization`, `domain`,
  `email`, `username`.
- **Bounds:** Robin's defaults: at most 50 results, 10 pages read, 8000 characters each,
  4 threads. The default timeout is 900 s, capped by `HARVEST_TOOL_TIMEOUT` and the job's
  remaining wall clock. Robin's own CSAM-title filter stays on.

## Records

Capture body `{"tool": "robin", "target", "results": [...]}`:

- one `report` record: query, refined query, preset, status (`ok`, `no_results`,
  `engines_unreachable`, `nothing_relevant`, `nothing_readable`), summary, pivots, per-engine
  search stats
- one record per search result: `url`, `title`, `engine`, `selected` (kept by the model),
  `excerpt` (scraped text, when read), `platform: "tor"`

Attribution follows the `apify_*` rule: `existence` observed, `ownership` candidate.

## Changes

| File | Change |
|---|---|
| `src/harvest/robin_driver.py` | new: runs under Robin's venv; JSON on stdout, logs on stderr |
| `src/harvest/tools.py` | `_robin` runner, records, `TOOLS["robin"]`, attribution |
| `src/harvest/models.py` | free-text target pattern for `robin` |
| `src/harvest/config.py` | `HARVEST_TOR_PROXY`, `HARVEST_ROBIN_HOME` (default `/opt/robin`), timeout 900 |
| `src/harvest/engine.py` | `crawl=False` for `robin` |
| `src/harvest/capabilities.py`, `web/app.js` | evidence/cost note, UI label |
| `Dockerfile` | `ARG HARVEST_ROBIN_REF=""`; when set, clone and install into `/opt/robin` |
| `deploy/ovh-vps/compose.robin.yaml`, `deploy/ovh-vps/tor/` | Tor sidecar |
| `README.md`, `OPERATIONS.md`, `.env.example` | usage and settings |
| `tests/test_robin_tool.py` | new |

## Validation

1. `tests/test_robin_tool.py`: a fake Robin home whose driver prints canned JSON, exercised
   through the real `_exec`: records, the model/Tor/not-enabled policy denials, target
   validation, a nonzero exit, and a job that captures without queueing `fetch` tasks.
2. Driver check against the real pinned Robin, with Robin's own stub search/scrape/LLM
   (`robin/tests/stubs.py`), in a throwaway venv, so the JSON contract is proven against
   real Robin code.
3. `uv run pytest -q` and `uv run ruff check src tests`.
4. Manual: build the image with `HARVEST_ROBIN_REF`, bring up the Tor sidecar, run one query
   against a configured model. This needs a model endpoint and makes real Tor requests, so
   it waits for your go-ahead.

## Risks and follow-ups

- **Robin's model calls bypass Harvest's model budget.** Harvest meters its own calls
  through `store.reserve`; Robin calls the endpoint directly (about 4 calls per run). Bounded
  only by the run limits above. Follow-up if this matters: a metering proxy in front of the
  model.
- **The Tor container egresses directly to the Tor network** from the host's address. Onion
  services never see it, but proxy-only mode does not cover it. Documented, not enforced.
- **The upstream pin is not a package.** Upgrading means changing `HARVEST_ROBIN_REF` and
  re-running step 2 above.

## Rollback

Remove `robin` from `HARVEST_TOOLS` and drop the `compose.robin.yaml` overlay. No schema
changes.
