# github-core-clone — v2

A standalone GitHub-compatible git forge in a single Python 3.13 file
(`main.py`, stdlib only — no installs, no git binary, no network beyond
the suite). v2 is a performance rewrite of the v1 entry: same wire
behaviour, same 120/120 conformance, measurably faster on the benchmark
workloads.

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

## v2 performance work (vs v1, kit local runner, same VM)

| workload | v1 p50 | v2 p50 | v1 pts | v2 pts |
|----------|--------|--------|--------|--------|
| push     | 516 ms | 295 ms | 46 | 50 |
| clone    | 153 ms | 112 ms | 35 | 41 |
| read     | 899 ms | 879 ms | 45 | 46 |
| write    | 141 ms | 124 ms | 41 | 44 |
| **total** | | | **167** | **181** |

Local score: **981/1000** (v1: 967), feature 600/600, agent 200/200,
0 failed checks out of 120. Official v1 was 925 (perf 125); the local
numbers are a relative yardstick — production runs ~1.8x slower than
this VM, so expect the official v2 perf to land around 135-145.

What changed, in one paragraph: parsed git objects are memoized by oid
(objects never change, so the memo never goes stale); the fetch BFS
stops at the deepen limit instead of walking the whole history; an
identical fetch reuses a memoized pack, so the 50 concurrent depth-1
clones share one pack build; the fast-forward check walks parents from
the new tip and stops at the old tip; packs are written with zlib level
1 (same wire format, much faster); responses are assembled as one bytes
object and written once; the router skips query parsing when a request
carries no query string. Nothing about the wire behaviour changed —
the same 120 checks pass on the same server.
