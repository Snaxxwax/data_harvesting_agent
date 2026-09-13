# Repository handoff

The canonical repository is
[Snaxxwax/data_harvesting_agent](https://github.com/Snaxxwax/data_harvesting_agent).
The 0.1 implementation was published in commit `d66f3d33f5926ac50e277df8df83200eda7fedfd`.
The initial inability to create a repository is resolved; old transfer archives describe
0.1 and are not the source of truth for later work.

Clone with your normally configured GitHub authentication:

```bash
git clone https://github.com/Snaxxwax/data_harvesting_agent.git
cd data_harvesting_agent
uv sync --frozen
uv run pytest -q
```

Changes are published with the existing branch head as parent and without forced updates.
The GitHub Actions workflow tests, builds distributions, and builds the Docker image.
Inspect its actual conclusion before deployment; a commit does not imply successful CI
or a running service. Runtime databases, local environments and credentials are not committed.
