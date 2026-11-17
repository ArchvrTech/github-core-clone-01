[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/ArchvrTech/github-core-clone-01/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/ArchvrTech/github-core-clone-01/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

# github-core-clone — v3

Built for [make.fun's "Build a GitHub-compatible git forge" bounty](https://make.fun/bounty/github-core-clone-01).

A standalone GitHub-compatible git forge in a single Python 3.13 file
(`main.py`, stdlib only — no installs, no git binary, no network beyond
the suite). v3 is a second performance pass over v2: same wire behaviour,
same 120/120 conformance, with the HTTP layer itself rebuilt for speed.

## What it serves

- **Git smart HTTP** at `/{owner}/{repo}.git` — upload-pack protocol v2
  (ls-refs, fetch with shallow/deepen/haves) with v0 fallback, and
  receive-pack v0 with report-status, side-band-64k, delete-refs and
  fast-forward-only per-ref ok/ng reports. Pure-Python git object store
  (loose objects, pack read/write with OFS/REF deltas, pkt-line framing).
- **REST v3** under `/api/v3` — repos, branches, commits, refs, contents
  (GET/PUT/DELETE), issues, pull requests (squash merge with three-way
  conflict detection), users, rate limit, meta, root.
- **GraphQL v4** at `/api/graphql` — viewer, repository, node, `__type`
  introspection, issues/pullRequests connections with cursor pagination,
  createIssue / closeIssue / createPullRequest / mergePullRequest /
  addComment mutations, batched requests.
- **MCP** at `/mcp` — JSON-RPC 2.0 Streamable HTTP, revision 2026-07-28
  (`server/discover`, `initialize`, `tools/list`, `tools/call`) with six
  tools: `create_repo`, `get_repo`, `create_issue`, `list_issues`,
  `create_pull_request`, `merge_pull_request`.

## Auth

Bearer token from the `MAKEFUN_SHOP_TOKEN` environment variable. Missing
or wrong token is 401; a repo owned by anyone else is 404 (never 403).
Every REST reply is `application/json` with a non-empty
`X-GitHub-Request-Id`.

## Running

With an env (the graded mode):

```
python3 main.py          # binds port 0, POSTs $MAKEFUN_ENV_URL/register,
                         # polls $MAKEFUN_ENV_URL/done, exits 0
```

Standalone (local testing, no env):

```
MAKEFUN_SHOP_TOKEN=tok PORT=8765 python3 main.py
```

Determinism: all timestamps are fixed (`FIXED_TS`); the in-memory store
is the single source of truth, so a replayed run produces the same state.

## v3 performance work (vs v2, kit local runner, same VM)

| workload | v2 p50 | v3 p50 | v2 pts | v3 pts |
|----------|--------|--------|--------|--------|
| push     | ~120 ms | ~91 ms | 50 | 50 |
| clone    | ~65 ms | ~38 ms | 49–50 | 50 |
| read     | ~345 ms | ~192 ms | 50 | 50 |
| write    | ~40 ms | ~26 ms | 50 | 50 |
| **p50_us** | **~102,500** | **~63,400** | | |

Local score: **1000/1000** (v2: 999–1000 on the same quiet VM, 981 when
loaded), feature 600/600, agent 200/200, 0 failed checks out of 120, three
consecutive fresh-server runs. The tie-break p50_us fell ~1.6x vs v2
(102,500 → 63,400), at parity with the Go rewrite's 63,595.

What changed, in one paragraph: the HTTP layer is a hand-rolled raw-socket
server (one thread per connection, minimal request-line/header parsing)
instead of `BaseHTTPRequestHandler`, whose per-request header parsing
through the email package cost ~80 µs per request; the Date header is
computed once a second and cached; issue, repository and static JSON
responses are serialized once and cached as bytes (a repeated GET of the
same issue is a dict lookup plus one send); the git v2 advertisement is a
module-level constant; the router works on the raw path string. Nothing
about the wire behaviour changed — the same 120 checks pass on the same
server.
