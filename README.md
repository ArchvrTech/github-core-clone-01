# github-core-clone

A standalone GitHub-compatible git forge in a single Python 3.13 file
(`main.py`, stdlib only — no installs, no git binary, no network beyond
the suite).

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
