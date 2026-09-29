# airdress-home

Link a home hub such as [Home Assistant](https://www.home-assistant.io/) to an
[airdress](https://airdress.co), so that functions running on your airdress's
operator can operate and observe exactly the entities you chose to share — and
nothing else.

This is the protocol library the Home Assistant integration uses. It has no
Home Assistant dependency: it is plain `asyncio` on `aiohttp` and
`cryptography`, strictly typed.

> **Status: beta (0.1.0b1).** Both channel transports stay and are
> negotiated; 0.1.0 follows once their default order and fallback thresholds
> are measured, and the API may change until then.

## What it does

- **Enrollment as a machine.** The hub generates an Ed25519 key and asks the
  operator to enroll it. The owner compares a confirmation code and approves on
  the operator. The hub never receives a bearer or a secret: it signs each
  request with its own key (RFC 9421, `airdress-machine` tag).
- **A pinned operator key.** The operator signs its enrollment answer; the hub
  verifies it, and from then on accepts a frame only if it verifies under that
  same key.
- **One held channel, dialled by the hub.** The hub is behind NAT and the
  operator cannot dial it. The hub keeps a channel open, and the operator sends
  it signed frames: `call` and `read`, which the hub answers, `features` (what
  the operator's Home declares) and `emit` (an event for the hub).
- **Multi-transport.** Every transport carries the same signed frames, `seq`
  and session, and each stays:
  - `channel.WsChannel` — a WebSocket;
  - `channel.PollChannel` — a streaming long-poll, rotated before a relay's
    idle timeout, with batched upstream requests;
  - `channel.NegotiatingChannel` — what `channel.for_client` returns: it tries
    the preferred transport first, falls back when its establishment is
    refused on the way or it keeps dropping early, remembers per network what
    worked (a `HintStore`; `FileHintStore` keeps it in one small file, holding
    no address), and probes the preferred transport again after a while.
- **The rendezvous.** "Sign in with Airdress": the hub (`account.airdress.co`)
  introduces the hub to the owner's operator without anyone typing an address.
  It never approves and never sees a key.

Every operator frame carries a session, a strictly increasing `seq` and a
`notAfter`; a repeated `seq` is dropped and a gap is counted.

## Using it

```python
import aiohttp
from airdress_home import MachineKey, MachineClient, HomeSession, start_enrollment, poll_until_decided
from airdress_home.channel import FileHintStore, for_client

async with aiohttp.ClientSession() as http:
    key = MachineKey.generate()
    started = await start_enrollment(http, "https://<your airdress>", key, "Home Assistant")
    print("Confirm on your operator:", started.user_code, started.confirmation_code)
    enrollment = await poll_until_decided(http, "https://<your airdress>", key, started)

    client = MachineClient(http, key, enrollment)
    channel = for_client(client, hints=FileHintStore("transport-hints.json"))
    session = HomeSession(channel, handler, enrollment.pinned_key)
    await session.run()
```

`handler` implements `airdress_home.Handler`: `call`, `read`, `shared`,
`features` and `emit`. The session keeps the hub's own ceilings whatever the
operator sends (60 calls and 60 emits a minute by default), and
`airdress_home.is_sensitive` names the entities — locks, alarm panels, and
entry-point or unclassified covers — that the hub must refuse to operate unless
its user opted each one in. Applications hold `channel.for_client(client)`
rather than naming a transport; `channel.name` is the transport in use.

## The `airdress` package

The PyPI project [`airdress`](https://pypi.org/project/airdress/) is built from
[`airdress/`](airdress/) in this repository: a small meta-package that installs
`airdress-home` and makes `import airdress.home` that package.

## Development

```sh
uv sync
uv run pytest
uv run mypy
prek install   # the same checks CI runs, and the commit-msg hook
```

`tests/vectors/vectors.json` is shared with the operator: every value in it is
recomputed by both implementations.

### Commits and releases

Commit messages and PR titles are
[conventional commits](https://www.conventionalcommits.org/en/v1.0.0/)
(`fix: …`, `feat: …`, `docs: …`, `feat!: …` for a breaking change). The
commit-msg hook checks each commit, and CI checks a PR's title and commits.

Releases are made by [release-please](https://github.com/googleapis/release-please),
never by hand:

1. Every push to `main` updates one open release PR, `chore(main): release
   <version>`, with the next version in `pyproject.toml` and
   `.release-please-manifest.json`, `uv.lock` re-locked, and the new
   `CHANGELOG.md` entry. A `fix:` or `feat:` commit makes one; `docs:`,
   `chore:`, `ci:` and the like do not on their own.
2. Merging that PR tags `vX.Y.Z-bN` and creates a draft GitHub release.
3. The tag starts `release.yml`: it checks the tag against the version,
   tests, builds and publishes both packages to PyPI by trusted publishing,
   then publishes the draft release.

Until 0.1.0 every version is a beta: `fix:`, `feat:` and even a breaking
change all move `0.1.0-b3` to `0.1.0-b4`. release-please spells it with a
hyphen; every Python tool reads it as the PEP 440 version `0.1.0b4`, which is
what PyPI shows. To leave the betas, put `Release-As: 0.1.0` in the body of a
commit on `main`, and remove `versioning`, `prerelease` and `prerelease-type`
from `release-please-config.json`.

The `airdress` meta-package is not part of this cycle. It keeps its own
version (0.0.0), which changes only when its own API does: bump it by hand in
`airdress/pyproject.toml` in an ordinary PR, and the next release tag
publishes it with `airdress-home`. A release that leaves it alone skips it.

## Licence

Apache License 2.0.
