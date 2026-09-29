# Changelog

Versions are PEP 440 pre-releases until 0.1.0. release-please writes them in
the form `0.1.0-b4`, which every Python tool reads as `0.1.0b4`, the version
PyPI shows. Entries from 0.1.0-b4 on are generated from conventional commits;
the ones below were written by hand from the history before that.

## [0.1.0-b4](https://github.com/airdress-co/airdress-home/compare/v0.1.0-b3...v0.1.0-b4) (2026-09-29)


### Bug Fixes

* __version__ is the installed version ([7891578](https://github.com/airdress-co/airdress-home/commit/78915786d4232b316b378d47a12f37b526972599))

## 0.1.0b3 (2026-09-29)

The first version published to PyPI. 0.1.0b1 was never tagged and 0.1.0b2
was tagged but never published: hatchling 1.32 writes Metadata-Version 2.5,
which the publish action refuses, so the build now pins `hatchling<1.32`.

### Features

* **machine:** enrollment as a machine with `purpose: home-assistant`,
  request signing (RFC 9421) with only the headers that were signed, and the
  operator's key pinned from its signed enrollment answer.
* **machine:** `start_reauth`, so a machine whose approval lapsed asks to be
  approved again and keeps its identity.
* **rendezvous:** "Sign in with Airdress", following the hub's contract at
  `account.airdress.co`.
* **session:** the held channel with the operator's frame bodies (`hello`,
  `features`, `track`, `notify`, close codes), emits, the hub's own ceilings
  and the sensitive-entity set.
* **channel:** multi-transport: WebSocket and streaming long-poll, negotiated,
  with fallback, a per-network hint and a re-probe; a channel is timed from
  its session's `hello`.
* The `airdress` meta-package (0.0.0): `import airdress.home` is
  `airdress_home`.

### Bug Fixes

* **session:** a 401 on a redial is terminal, handed on as revoked or lapsed.
