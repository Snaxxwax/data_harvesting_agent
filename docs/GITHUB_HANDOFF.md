# Repository handoff

The authenticated GitHub account is `Snaxxwax`. The currently exposed GitHub connector
supports file/branch/commit writes to existing repositories but does not expose repository
creation. No CLI credential is available in this workspace. This is a capability limitation,
not a permission request or an automatic approval rejection.

Create an empty private repository named `harvest-platform` (or choose another name).
Avoid initializing it with README/license/gitignore because this project already has them.
Once its URL is available, the prepared local Git history can be pushed and the included
GitHub Actions workflow can verify the build, including the Docker image.

If transferring the downloadable Git bundle to another machine:

```bash
git clone harvest-platform.bundle harvest-platform
cd harvest-platform
git remote remove origin
git remote add origin git@github.com:Snaxxwax/harvest-platform.git
git push -u origin main
```

The source ZIP is also usable as a normal source checkout. Run `uv sync --frozen` and the
commands in README. Neither archive includes secrets, runtime databases, virtual
environments or the failed intermediate network experiments.
