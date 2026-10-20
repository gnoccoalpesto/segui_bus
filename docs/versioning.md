# Versioning

The repository version is tracked in a single file: `VERSION` at the repo root.

```
0.1.0
```

That file is the **only** source of truth. Code, config and docs must read it or
reference it; no version string is duplicated or hardcoded anywhere else.

## Scheme

[Semantic Versioning](https://semver.org/), `MAJOR.MINOR.PATCH`, pre-1.0:

- `0.x.y` — pre-stable; the public interface may still change between minors.
- **MINOR** — a new capability or a behaviour change (new trigger rule, new
  transport, new output channel, breaking config change while `0.x`).
- **PATCH** — fixes that do not change behaviour (bug fix, docs-only change that
  does not alter the interface).
- **MAJOR** — reserved for the first stable release and for breaking changes
  after that.
- Pre-release suffix for experiments: `0.2.0-rc.1` (never a release tag).

## Where the version is used

1. **HTTP identification** — `X-BusOne-Client: segui_bus/<VERSION> gh/gnoccoalpesto`
   (see `docs/research/2026-10-20-busone-proxy-seta-pc.md` §2.4).
2. **Startup log** — one line: `segui_bus <VERSION> · seta-pc · personal`.
3. **Home Assistant** — device `sw_version` once there is a device.
4. **Message footer** — optional, together with the attribution line.

## Release procedure

```bash
# 1. bump the single source of truth
printf '%s\n' 0.2.0 > VERSION

# 2. keep docs in sync if the interface changed
git add VERSION

# 3. commit and tag with the same value
git commit -m "chore(release): v0.2.0"
git tag -a v0.2.0 -m "v0.2.0"
git push origin main --tags
```

Rules:

- never tag a commit whose `VERSION` differs from the tag (strip the leading `v`);
- bump **before** merging a feature, not in a separate "release" PR;
- if a change touches the interface, the bump and the docs change ship together;
- no auto-generated versions, no timestamps, no git-describe in the header — the
  value BusOne sees must be a release the repo can point at.

## Adding the version to a new component

Read `VERSION` at build time and pass it down; do not parse it from the git tag.
For example:

```python
from pathlib import Path

VERSION = Path(__file__).resolve().parents[1].joinpath("VERSION").read_text().strip()
CLIENT_HEADER = f"segui_bus/{VERSION} gh/gnoccoalpesto"
```
