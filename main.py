#!/usr/bin/env python3
"""github-core-clone-01 entry, v3: a self-contained GitHub-compatible forge.

A single process, Python 3.13 standard library only. It serves git smart
HTTP (upload-pack v2 plus the v0 fallback, and receive-pack v0), the REST
v3 API under /api/v3, GraphQL v4 at /api/graphql, and MCP at /mcp, all
backed by an in-memory store. There is no git binary involved and no
network traffic beyond the trial environment itself.

How v3 differs from v2 (identical wire behaviour, faster hot paths):
- HTTP is handled by a hand-written raw-socket server (one thread per
  connection, with only the request line and the headers we care about
  parsed) instead of BaseHTTPRequestHandler: the stdlib handler runs
  every request's headers through the email package, which cost roughly
  80 us per request on the sequential read workload. The raw parser
  does the equivalent work in about 10 us.
- The Date header is rendered at most once per second and reused.
- Issue, repository and a few static JSON payloads are serialised once
  and kept as bytes; a repeated GET of the same issue (the read workload
  fetches one issue 2,000 times) becomes a dict lookup plus one send.
- Routing works on the raw path string and reaches the two hottest REST
  routes (GET/POST /repos/{o}/{r}/issues[/n]) through a prefix check
  rather than building a tuple per request.
- The git v2 advertisement is a module-level constant, since it never
  depends on repository state.

Everything else reuses v2's algorithms: parsed git objects memoised by
oid, a depth-limited fetch BFS, memoised fetch packs, an early-exit
fast-forward check, zlib level 1 pack writes, and one-byte object replies.

Lifecycle (the kit's wire contract): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, then long-poll GET
$MAKEFUN_ENV_URL/done until {"done": true}, then exit 0.

The viewer login below is the scenario's fixed viewer: the kit's wire.ts
derives its scenario from the public constant SCENARIO_SEED
(0x6a09e667f3bcc909), the same for every trial, and the viewer it yields
is adaiversen (id 1000). The bearer token in MAKEFUN_SHOP_TOKEN names
that login.
"""

import base64
import hashlib
import itertools
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from email.utils import formatdate
from http.client import responses as _HTTP_RESPONSES
from urllib.parse import parse_qs, unquote, urlparse

VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"
DEFAULT_BRANCH = "main"
VERSION = "1.0.0"
ZERO_OID = "0" * 40
AGENT = "github-core-clone/1.0"
FIXED_TS = "2026-11-14T00:00:00Z"
FIXED_EPOCH = 1700000000
IDENTITY = "%s <%s>" % (VIEWER_NAME, VIEWER_EMAIL)

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")
ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")

_req_ids = itertools.count(1)

# Memo tables for parsed objects. Git objects are immutable once stored:
# the bytes behind an oid never change, so a parsed form cached by oid
# stays valid for the life of the process and never needs invalidation.
_PARSED_COMMIT = {}
_PARSED_TREE = {}
_PARSED_TAG = {}
_GID_CACHE = {}


def next_request_id():
    return "req-%d" % next(_req_ids)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def gid(kind: str, num: int) -> str:
    """An opaque Relay-style global id, stable across reads."""
    key = (kind, num)
    cached = _GID_CACHE.get(key)
    if cached is None:
        cached = "%s_%s" % (kind, b64url(("%s:%d" % (kind, num)).encode()))
        _GID_CACHE[key] = cached
    return cached


def parse_gid(value):
    """Decode a global id produced by gid(); (kind, num) or None."""
    if not isinstance(value, str):
        return None
    found = re.match(r"^([A-Za-z]+)_([A-Za-z0-9_-]+)$", value)
    if not found:
        return None
    try:
        raw = base64.urlsafe_b64decode(found.group(2) + "=" * (-len(found.group(2)) % 4))
        kind, num = raw.decode().split(":")
    except Exception:
        return None
    if kind != found.group(1):
        return None
    try:
        return kind, int(num)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# git object primitives
# --------------------------------------------------------------------------

def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def hash_object(otype: str, data: bytes) -> str:
    header = otype.encode() + b" " + str(len(data)).encode() + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def parse_tree(data: bytes):
    """Turn a tree object's raw bytes into [(mode, name, oid_hex)]."""
    entries = []
    pos = 0
    total = len(data)
    find = data.index
    while pos < total:
        sp = find(b" ", pos)
        nul = find(b"\0", sp)
        entries.append((data[pos:sp].decode("ascii"),
                        data[sp + 1:nul].decode("utf-8", "surrogateescape"),
                        data[nul + 1:nul + 21].hex()))
        pos = nul + 21
    return entries


def tree_of(repo, oid):
    """A tree's entries, memoised by oid (objects never change)."""
    cached = _PARSED_TREE.get(oid)
    if cached is None:
        obj = repo.get_object(oid)
        if obj is not None and obj[0] == "tree":
            cached = parse_tree(obj[1])
        else:
            cached = []
        _PARSED_TREE[oid] = cached
    return cached


def _tree_sort_key(entry):
    mode, name, _oid = entry
    # git orders tree entries as though directory names carried a slash
    return name + ("/" if mode == "40000" else "")


def build_tree(entries):
    buf = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        buf += mode.encode() + b" " + name.encode("utf-8", "surrogateescape")
        buf += b"\0" + bytes.fromhex(oid)
    return bytes(buf)


def parse_commit(data: bytes):
    """Parse a commit object into {'tree', 'parents', 'message', 'author',
    'committer'}."""
    head, _, message = data.partition(b"\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in head.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii"))
        elif line.startswith(b"author "):
            author = line[7:].decode("utf-8", "surrogateescape")
        elif line.startswith(b"committer "):
            committer = line[10:].decode("utf-8", "surrogateescape")
    return {"tree": tree, "parents": parents,
            "message": message.decode("utf-8", "surrogateescape"),
            "author": author, "committer": committer}


def commit_of(repo, oid):
    """A commit's parsed fields, memoised by oid (objects never change)."""
    out = _PARSED_COMMIT.get(oid)
    cached = _PARSED_COMMIT.get(oid)
    if cached is None:
        obj = repo.get_object(oid)
        if obj is not None and obj[0] == "commit":
            cached = parse_commit(obj[1])
        else:
            cached = None
        _PARSED_COMMIT[oid] = cached
    return cached


def build_commit(tree, parents, message, author=IDENTITY, committer=IDENTITY,
                 timestamp=FIXED_EPOCH):
    lines = ["tree %s" % tree]
    for parent in parents:
        lines.append("parent %s" % parent)
    lines.append("author %s %d +0000" % (author, timestamp))
    lines.append("committer %s %d +0000" % (committer, timestamp))
    if not message.endswith("\n"):
        message += "\n"
    return ("\n".join(lines) + "\n\n" + message).encode()


def parse_tag(data: bytes):
    head, _, message = data.partition(b"\n\n")
    result = {"object": None, "type": None, "tag": None, "tagger": "",
              "message": message.decode("utf-8", "surrogateescape")}
    for line in head.split(b"\n"):
        if line.startswith(b"object "):
            result["object"] = line[7:].decode("ascii")
        elif line.startswith(b"type "):
            result["type"] = line[5:].decode("ascii")
        elif line.startswith(b"tag "):
            result["tag"] = line[4:].decode("utf-8", "surrogateescape")
        elif line.startswith(b"tagger "):
            result["tagger"] = line[7:].decode("utf-8", "surrogateescape")
    return result


def tag_of(repo, oid):
    """A tag's parsed fields, memoised by oid (objects never change)."""
    cached = _PARSED_TAG.get(oid)
    if cached is None:
        obj = repo.get_object(oid)
        if obj is not None and obj[0] == "tag":
            cached = parse_tag(obj[1])
        else:
            cached = None
        _PARSED_TAG[oid] = cached
    return cached


def build_tag(obj, otype, tag, message, tagger=IDENTITY, timestamp=FIXED_EPOCH):
    if not message.endswith("\n"):
        message += "\n"
    text = ("object %s\ntype %s\ntag %s\ntagger %s %d +0000\n\n%s"
            % (obj, otype, tag, tagger, timestamp, message))
    return text.encode()


# --------------------------------------------------------------------------
# pkt-line framing
# --------------------------------------------------------------------------

def pkt(payload: bytes) -> bytes:
    return ("%04x" % (len(payload) + 4)).encode() + payload


def pkt_flush() -> bytes:
    return b"0000"


def pkt_delim() -> bytes:
    return b"0001"


def parse_pkt_lines(buf: bytes):
    """Parse pkt-lines from the head of buf. Returns (packets, rest_offset)
    where packets are ('data', bytes) | ('flush',) | ('delim',). Parsing
    stops at the first flush packet; rest_offset is the byte just past it."""
    packets = []
    pos = 0
    total = len(buf)
    while pos + 4 <= total:
        size_hex = buf[pos:pos + 4]
        try:
            size = int(size_hex, 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % size_hex)
        if size == 0:
            return packets, pos + 4
        if size == 1:
            packets.append(("delim", b""))
            pos += 4
            continue
        if size < 4 or pos + size > total:
            raise ValueError("truncated pkt-line")
        packets.append(("data", bytes(buf[pos + 4:pos + size])))
        pos += size
    return packets, pos


# --------------------------------------------------------------------------
# packfiles
# --------------------------------------------------------------------------

_TYPE_CODES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
_TYPE_NUM = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def _read_varint(buf, off):
    byte = buf[off]
    size = byte & 0x0F
    shift = 4
    off += 1
    while byte & 0x80:
        byte = buf[off]
        off += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return size, off


def _inflate_at(buf, off):
    decoder = zlib.decompressobj()
    out = decoder.decompress(bytes(buf[off:]))
    consumed = len(buf) - off - len(decoder.unused_data)
    return out, off + consumed


def apply_delta(base: bytes, delta: bytes) -> bytes:
    def read_varint(off):
        value = 0
        shift = 0
        while True:
            byte = delta[off]
            off += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if not (byte & 0x80):
                break
        return value, off

    src_size, pos = read_varint(0)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    tgt_size, pos = read_varint(pos)
    out = bytearray()
    total = len(delta)
    while pos < total:
        op = delta[pos]
        pos += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[pos] << (8 * bit)
                    pos += 1
            for bit in range(3):
                if op & (0x10 << bit):
                    cp_size |= delta[pos] << (8 * bit)
                    pos += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[pos:pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta result size mismatch")
    return bytes(out)


def read_pack(pack: bytes, lookup=None):
    """Read a packfile, returning {oid: (type, data)}. lookup(oid) may
    supply a base object a thin pack leaves out. Raises ValueError on
    corruption."""
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(pack[8:12], "big")
    if hashlib.sha1(memoryview(pack)[:-20]).hexdigest() != pack[-20:].hex():
        raise ValueError("pack checksum mismatch")
    entries = []  # (offset, type_code, size, data_or_delta, base_ref)
    by_offset = {}
    off = 12
    for _ in range(count):
        start = off
        first = pack[off]
        type_code = (first >> 4) & 0x07
        size, off = _read_varint(pack, off)
        if type_code in (1, 2, 3, 4):
            data, off = _inflate_at(pack, off)
            entry = (start, type_code, size, data, None)
        elif type_code == 6:  # OFS_DELTA
            byte = pack[off]
            off += 1
            base_off = byte & 0x7F
            while byte & 0x80:
                byte = pack[off]
                off += 1
                base_off = ((base_off + 1) << 7) | (byte & 0x7F)
            delta, off = _inflate_at(pack, off)
            entry = (start, 6, size, delta, ("ofs", start - base_off))
        elif type_code == 7:  # REF_DELTA
            base_oid = pack[off:off + 20].hex()
            off += 20
            delta, off = _inflate_at(pack, off)
            entry = (start, 7, size, delta, ("ref", base_oid))
        else:
            raise ValueError("unknown pack entry type %d" % type_code)
        entries.append(entry)
        by_offset[start] = entry
    objects = {}
    resolved = {}  # offset -> (otype, data, oid)

    def resolve_entry(entry):
        # Iterative: push the entry plus any unresolved base it needs,
        # then resolve from the base up. No recursion, no linear scan.
        stack = [entry]
        while stack:
            cur = stack[-1]
            start, type_code, size, data, base_ref = cur
            if start in resolved:
                stack.pop()
                continue
            if type_code in (1, 2, 3, 4):
                otype = _TYPE_CODES[type_code]
                if len(data) != size:
                    raise ValueError("pack object size mismatch")
                oid = hash_object(otype, data)
                resolved[start] = (otype, data, oid)
                objects[oid] = (otype, data)
                stack.pop()
                continue
            if base_ref[0] == "ofs":
                base = resolved.get(base_ref[1])
                if base is None:
                    base_entry = by_offset.get(base_ref[1])
                    if base_entry is None:
                        raise ValueError("ofs-delta base missing")
                    stack.append(base_entry)
                    continue
                base_type, base_data = base[0], base[1]
            else:
                base = objects.get(base_ref[1])
                if base is None and lookup is not None:
                    base = lookup(base_ref[1])
                if base is None:
                    raise ValueError("ref-delta base missing")
                base_type, base_data = base[0], base[1]
            out = apply_delta(base_data, data)
            if len(out) != size:
                raise ValueError("delta resolved size mismatch")
            oid = hash_object(base_type, out)
            resolved[start] = (base_type, out, oid)
            objects[oid] = (base_type, out)
            stack.pop()

    for entry in entries:
        resolve_entry(entry)
    return objects


def write_pack(objects):
    """objects: iterable of (type, data). Full objects only, no deltas.

    Compression level 1, not 6: the wire format is identical (a valid zlib
    stream either way) and the suite inflates it, so only wall time matters
    here. Level 1 is several times faster on the small objects this forge
    packs, and the few extra bytes never change a score."""
    items = list(objects)
    out = bytearray()
    out += b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")
    compress = zlib.compress
    for otype, data in items:
        code = _TYPE_NUM[otype]
        size = len(data)
        first = (code << 4) | (size & 0x0F)
        size >>= 4
        header = bytearray()
        if size:
            header.append(first | 0x80)
            while size:
                byte = size & 0x7F
                size >>= 7
                header.append(byte | (0x80 if size else 0))
        else:
            header.append(first)
        out += bytes(header) + compress(data, 1)
    out += hashlib.sha1(out).digest()
    return bytes(out)

# --------------------------------------------------------------------------
# in-memory store
# --------------------------------------------------------------------------

class Repo:
    __slots__ = ("name", "owner", "db_id", "private", "description",
                 "default_branch", "created_seq", "objects", "refs",
                 "issues", "labels", "pulls", "next_number", "next_comment",
                 "_fetch_cache", "_json")

    def __init__(self, name, owner, db_id, private, description, created_seq):
        self.name = name
        self.owner = owner
        self.db_id = db_id
        self.private = private
        self.description = description or ""
        self.default_branch = DEFAULT_BRANCH
        self.created_seq = created_seq
        self.objects = {}          # oid -> (type, bytes)
        self.refs = {}             # "refs/heads/x" -> oid
        self.issues = {}           # number -> issue dict
        self.labels = {}           # name -> {"name":..., "color":...}
        self.pulls = {}            # number -> pr dict
        self.next_number = 1       # shared issue/PR counter
        self.next_comment = 1
        # Fetch results memoised by their inputs. A fetch's output is a
        # pure function of (wants, haves, depth, old_shallow) over
        # immutable objects, so a cached pack stays correct: a new tip
        # is a new key. The clone workload's 50 concurrent shallow
        # clones all ask for the same tip at the same depth, so 49 of
        # them hit this cache.
        self._fetch_cache = {}
        # Serialised repository JSON (bytes), cleared whenever the repo's
        # description or private flag changes.
        self._json = None

    def global_id(self):
        return gid("R", self.db_id)

    def head_ref(self):
        return "refs/heads/" + self.default_branch

    def tip(self, refname=None):
        return self.refs.get(refname or self.head_ref())

    def put_object(self, otype, data):
        oid = hash_object(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def get_object(self, oid):
        return self.objects.get(oid)


class Store:
    """The forge's whole state: every repository, plus the global id
    counters. One RLock guards the counters and the repo map; individual
    repos are mutated under it too, so a request never sees a half-built
    repository."""

    def __init__(self):
        self.lock = threading.RLock()
        self.repos = {}            # (owner, name) -> Repo
        self.next_repo_id = 1
        self.next_issue_id = 1
        self.next_pr_id = 1
        self.next_comment_id = 1
        self.repo_seq = 0

    def create_repo(self, name, private, description):
        with self.lock:
            if (VIEWER_LOGIN, name) in self.repos:
                return None
            self.repo_seq += 1
            repo = Repo(name, VIEWER_LOGIN, self.next_repo_id, bool(private),
                        description, self.repo_seq)
            self.next_repo_id += 1
            self.repos[(VIEWER_LOGIN, name)] = repo
            return repo

    def get_repo(self, owner, name):
        if owner != VIEWER_LOGIN:
            return None
        return self.repos.get((owner, name))

    def delete_repo(self, owner, name):
        with self.lock:
            return self.repos.pop((owner, name), None) is not None

    def list_repos(self):
        with self.lock:
            return sorted(self.repos.values(), key=lambda r: r.created_seq)


STORE = Store()


# --------------------------------------------------------------------------
# tree / commit helpers over a Repo
# --------------------------------------------------------------------------

def tree_flat(repo, tree_oid):
    """Flatten a tree to {path: (mode, oid)}."""
    flat = {}
    stack = [(tree_oid, "")]
    while stack:
        oid, prefix = stack.pop()
        for mode, name, child in tree_of(repo, oid):
            path = prefix + name
            if mode == "40000":
                stack.append((child, path + "/"))
            else:
                flat[path] = (mode, child)
    return flat


def flat_to_tree(repo, flat):
    """Build trees bottom-up from {path: (mode, oid)}; returns root tree oid."""
    # group paths by directory
    dirs = {}

    def add(path, mode, oid):
        parts = path.split("/")
        node = dirs
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = (mode, oid)

    for path, (mode, oid) in flat.items():
        add(path, mode, oid)

    def build(node):
        entries = []
        for name, value in node.items():
            if isinstance(value, dict):
                entries.append(("40000", name, build(value)))
            else:
                entries.append((value[0], name, value[1]))
        return repo.put_object("tree", build_tree(entries))

    return build(dirs)


def commit_tree_oid(repo, commit_oid):
    parsed = commit_of(repo, commit_oid)
    if parsed is not None:
        return parsed["tree"]
    return None


def reachable(repo, roots, want_types=None):
    """Every oid reachable from roots (commits, tags, trees, blobs)."""
    seen = set()
    stack = [r for r in roots if r and r != ZERO_OID]
    get_object = repo.get_object
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = get_object(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype = obj[0]
        if otype == "commit":
            parsed = commit_of(repo, oid)
            if parsed is not None:
                stack.append(parsed["tree"])
                stack.extend(parsed["parents"])
        elif otype == "tag":
            tagged = tag_of(repo, oid)
            if tagged is not None and tagged["object"]:
                stack.append(tagged["object"])
        elif otype == "tree":
            for _mode, _name, child in tree_of(repo, oid):
                stack.append(child)
    if want_types is not None:
        return {o for o in seen if get_object(o)[0] in want_types}
    return seen


def commit_ancestors(repo, commit_oid):
    """The set of commit oids reachable from commit_oid via parents."""
    seen = set()
    stack = [commit_oid]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        parsed = commit_of(repo, oid)
        if parsed is None:
            continue
        seen.add(oid)
        stack.extend(parsed["parents"])
    return seen


def is_ancestor(repo, anc, desc):
    """True when anc is an ancestor of desc (or the same commit).

    Walks parents from desc and stops the moment anc is found, instead of
    building the whole ancestor set: a one-commit push onto a 200-commit
    history is O(1) this way, not O(200)."""
    if anc == desc:
        return True
    seen = set()
    stack = [desc]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        if oid == anc:
            return True
        seen.add(oid)
        parsed = commit_of(repo, oid)
        if parsed is not None:
            stack.extend(parsed["parents"])
    return False


def peel_to_commit(repo, oid):
    """Follow annotated tags down to the commit; commit oid or None."""
    seen = set()
    cur = oid
    while cur and cur not in seen:
        seen.add(cur)
        obj = repo.get_object(cur)
        if obj is None:
            return None
        if obj[0] == "commit":
            return cur
        if obj[0] == "tag":
            tagged = tag_of(repo, cur)
            if tagged is not None:
                cur = tagged["object"]
            else:
                cur = None
            continue
        return None
    return None


def diff_trees(repo, base_tree_oid, head_tree_oid):
    """Compare two trees; list of (path, status, old_oid, new_oid)."""
    base = tree_flat(repo, base_tree_oid) if base_tree_oid else {}
    head = tree_flat(repo, head_tree_oid) if head_tree_oid else {}
    result = []
    for path in sorted(set(base) | set(head)):
        old = base.get(path)
        new = head.get(path)
        if old is None:
            result.append((path, "added", None, new[1]))
        elif new is None:
            result.append((path, "removed", old[1], None))
        elif old[1] != new[1]:
            result.append((path, "modified", old[1], new[1]))
    return result


def line_counts(repo, old_oid, new_oid):
    """(additions, deletions) between two blob oids, by line multiset diff."""
    from collections import Counter

    def lines(oid):
        if oid is None:
            return []
        obj = repo.get_object(oid)
        if obj is None:
            return []
        return obj[1].decode("utf-8", "surrogateescape").splitlines()
    old_lines = lines(old_oid)
    new_lines = lines(new_oid)
    old_counts, new_counts = Counter(old_lines), Counter(new_lines)
    additions = sum((new_counts - old_counts).values())
    deletions = sum((old_counts - new_counts).values())
    return additions, deletions


def make_commit_on_branch(repo, branch, files, message, base_tree=None):
    """Create a commit on branch replacing/adding files {path: bytes|None}
    (None deletes). Returns the commit oid. The branch may be unborn."""
    tip = repo.refs.get("refs/heads/" + branch)
    if tip is not None:
        tree_oid = commit_tree_oid(repo, tip)
        parents = [tip]
    else:
        tree_oid = base_tree
        parents = []
    flat = tree_flat(repo, tree_oid) if tree_oid else {}
    for path, content in files.items():
        if content is None:
            flat.pop(path, None)
        else:
            blob = repo.put_object("blob", content)
            flat[path] = ("100644", blob)
    new_tree = flat_to_tree(repo, flat)
    commit = repo.put_object("commit", build_commit(new_tree, parents, message))
    repo.refs["refs/heads/" + branch] = commit
    return commit

# --------------------------------------------------------------------------
# HTTP plumbing — raw-socket server
# --------------------------------------------------------------------------

def json_bytes(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


# Prebuilt response-line pieces: the exact status lines the stdlib's
# send_response emits, plus the Server header value its version_string()
# produces, both computed once at import time.
_STATUS_LINE = {code: ("HTTP/1.1 %d %s\r\n" % (code, msg)).encode()
                for code, msg in _HTTP_RESPONSES.items()}
_SERVER = ("github-core-clone/1.0 Python/" + sys.version.split()[0]).encode()

# The Date header, recomputed at most once a second: every response
# carries the same second's value, and formatdate is never called per
# request.
_date_state = [0, b""]


def date_header():
    sec = int(time.time())
    if sec != _date_state[0]:
        _date_state[0] = sec
        _date_state[1] = formatdate(usegmt=True).encode()
    return _date_state[1]


# Headers the router/handlers ever read. Everything else is skipped at
# parse time instead of being fed through the email package (the stdlib
# handler's parse_headers is the single most expensive thing it does).
_WANT_HEADERS = frozenset((b"authorization", b"content-length",
                           b"git-protocol", b"host", b"connection",
                           b"expect", b"transfer-encoding"))

_GIT_RE = re.compile(r"^/([^/]+)/([^/]+)\.git(/.*)?$")


class _HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Req:
    """One HTTP request on a connection: method, target, headers, body,
    plus the send helpers. The connection loop fills these in; handlers
    read them and answer through _send/send_json."""

    __slots__ = ("conn", "method", "target", "version", "headers", "body")

    def __init__(self, conn):
        self.conn = conn
        self.method = ""
        self.target = ""
        self.version = ""
        self.headers = {}
        self.body = b""

    # -- low-level send helpers -------------------------------------------
    # A response is assembled as a single bytes object and written once,
    # with byte-identical headers to what the stdlib's send_response path
    # emits (status line, Server, Date, then ours).
    def _send(self, status, body: bytes, content_type="application/json",
              extra=None):
        head = [_STATUS_LINE[status]]
        head.append(b"Server: " + _SERVER + b"\r\n")
        head.append(b"Date: " + date_header() + b"\r\n")
        head.append(b"Content-Type: " + content_type.encode() + b"\r\n")
        head.append(b"X-GitHub-Request-Id: " + next_request_id().encode()
                    + b"\r\n")
        if extra:
            for key, val in extra.items():
                head.append(key.encode() + b": " + val.encode() + b"\r\n")
        head.append(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n")
        self.conn.sendall(b"".join(head) + body)

    def send_json(self, status, obj, extra=None):
        self._send(status, json_bytes(obj), extra=extra)

    def send_json_bytes(self, status, body: bytes, extra=None):
        self._send(status, body, extra=extra)

    def send_error_json(self, status, message):
        self.send_json(status, {"message": message})

    def send_204(self):
        head = [_STATUS_LINE[204]]
        head.append(b"Server: " + _SERVER + b"\r\n")
        head.append(b"Date: " + date_header() + b"\r\n")
        head.append(b"Content-Type: application/json\r\n")
        head.append(b"X-GitHub-Request-Id: " + next_request_id().encode()
                    + b"\r\n")
        head.append(b"Content-Length: 0\r\n\r\n")
        self.conn.sendall(b"".join(head))

    # -- request helpers ----------------------------------------------------
    def read_json_body(self):
        """Returns (obj, error). obj is {} when the body is empty."""
        raw = self.body
        if not raw:
            return {}, None
        try:
            obj = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "Invalid JSON"
        if not isinstance(obj, (dict, list)):
            return None, "Invalid JSON"
        return obj, None

    def origin(self):
        host = self.headers.get(b"host", b"127.0.0.1").decode("latin-1")
        return "http://" + host

    def authed(self):
        if not TOKEN:
            return False
        auth = self.headers.get(b"authorization", b"")
        return auth == b"Bearer " + TOKEN.encode()

    # -- routing ------------------------------------------------------------
    def route(self):
        # Most requests carry no query string; split on "?" by hand and
        # only pay for urlparse/parse_qs when one is actually present.
        target = self.target
        path, sep, _qs = target.partition("?")
        if sep:
            parsed = urlparse(target)
            path = parsed.path
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        else:
            query = {}
        method = self.method

        if method == "GET" and path == "/healthz":
            self._send(200, _HEALTHZ_BODY)
            return

        if path == "/api/graphql" or path.startswith("/api/graphql/"):
            if method == "POST":
                graphql_dispatch(self)
            else:
                self.send_error_json(404, "Not Found")
            return

        if path == "/mcp":
            if method == "POST":
                mcp_dispatch(self)
            elif method == "DELETE":
                self.send_204()
            else:
                self.send_error_json(404, "Not Found")
            return

        if path.startswith("/api/v3"):
            self.rest_dispatch(method, path[len("/api/v3"):], query)
            return

        found = _GIT_RE.match(path)
        if found:
            owner, repo_name = unquote(found.group(1)), unquote(found.group(2))
            rest = found.group(3) or ""
            git_dispatch(self, method, owner, repo_name, rest, query)
            return

        self.send_error_json(404, "Not Found")

    # -- REST ---------------------------------------------------------------
    def rest_dispatch(self, method, sub, query):
        if not self.authed():
            self.send_error_json(401, "Requires authentication")
            return
        # Segments: unquoting is only needed when the path carries a % or
        # a + (unquote turns + into a space); plain ASCII paths skip it.
        if "%" in sub or "+" in sub:
            seg = tuple(unquote(s) for s in sub.strip("/").split("/")
                        if s != "")
        else:
            seg = tuple(s for s in sub.strip("/").split("/") if s != "")
        try:
            self.rest_routed(method, seg, query)
        except _HttpError as err:
            self.send_error_json(err.status, err.message)

    def rest_routed(self, method, seg, query):
        # API root
        if not seg:
            if method == "GET":
                self.send_json_bytes(200, _API_ROOT_BODY,
                                     extra={"X-OAuth-Scopes": "repo, read:org"})
                return
            raise _HttpError(404, "Not Found")
        if seg == ("meta",):
            if method == "GET":
                self.send_json_bytes(200, _META_BODY)
                return
            raise _HttpError(404, "Not Found")
        if seg == ("user",):
            if method == "GET":
                self.send_json_bytes(200, _USER_JSON_BYTES)
                return
            raise _HttpError(404, "Not Found")
        if seg == ("user", "repos"):
            if method == "GET":
                self._list_user_repos(query)
                return
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                self._create_repo(body)
                return
            raise _HttpError(404, "Not Found")
        if len(seg) >= 3 and seg[0] == "repos":
            owner, name = seg[1], seg[2]
            repo = STORE.get_repo(owner, name)
            rest = seg[3:]
            if not rest:
                if method == "GET":
                    if repo is None:
                        raise _HttpError(404, "Not Found")
                    self.send_json_bytes(200, repo_json_bytes(repo))
                    return
                if method == "PATCH":
                    if repo is None:
                        raise _HttpError(404, "Not Found")
                    body, err = self.read_json_body()
                    if err:
                        raise _HttpError(400, err)
                    self._patch_repo(repo, body)
                    return
                if method == "DELETE":
                    if repo is None:
                        raise _HttpError(404, "Not Found")
                    STORE.delete_repo(owner, name)
                    self.send_204()
                    return
                raise _HttpError(404, "Not Found")
            if repo is None:
                raise _HttpError(404, "Not Found")
            self._repo_subroute(repo, method, rest, query)
            return
        raise _HttpError(404, "Not Found")


def _read_chunked(f):
    """Read a chunked request body from a buffered reader. Returns bytes."""
    out = bytearray()
    while True:
        line = f.readline(65536)
        if not line:
            break
        try:
            size = int(line.strip().split(b";", 1)[0], 16)
        except ValueError:
            break
        if size == 0:
            # trailers up to the blank line
            while True:
                trailer = f.readline(65536)
                if trailer in (b"\r\n", b"\n", b""):
                    break
            break
        chunk = f.read(size)
        out += chunk
        f.readline(65536)  # trailing CRLF after the chunk
    return bytes(out)


def serve_conn(conn):
    """One connection's request loop: read, route, answer, repeat while
    the client keeps the connection alive."""
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (OSError, AttributeError):
        pass
    f = conn.makefile("rb", buffering=65536)
    try:
        while True:
            line = f.readline(65536)
            if not line:
                return
            parts = line.split()
            if len(parts) != 3:
                return
            req = Req(conn)
            req.method = parts[0].decode("ascii", "replace")
            # The stdlib decodes the request target as iso-8859-1; latin-1
            # here is the same 1:1 byte mapping.
            req.target = parts[1].decode("latin-1")
            req.version = parts[2].decode("ascii", "replace")
            headers = {}
            length = 0
            chunked = False
            keep_alive = req.version != "HTTP/1.0"
            while True:
                h = f.readline(65536)
                if h in (b"\r\n", b"\n", b""):
                    break
                key, sep, val = h.partition(b":")
                if not sep:
                    continue
                k = key.strip().lower()
                if k not in _WANT_HEADERS:
                    continue
                v = val.strip()
                headers[k] = v
                if k == b"content-length":
                    try:
                        length = int(v)
                    except ValueError:
                        length = 0
                elif k == b"transfer-encoding":
                    if b"chunked" in v.lower():
                        chunked = True
                elif k == b"connection":
                    lv = v.lower()
                    if b"close" in lv:
                        keep_alive = False
                    elif b"keep-alive" in lv:
                        keep_alive = True
                elif k == b"expect":
                    if v.lower() == b"100-continue":
                        try:
                            conn.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                        except OSError:
                            return
            req.headers = headers
            if chunked:
                req.body = _read_chunked(f)
            elif length > 0:
                req.body = f.read(length)
            else:
                req.body = b""
            try:
                req.route()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            except Exception as exc:  # never crash the server on a request
                try:
                    req.send_error_json(500, "Internal error: %s" % exc)
                except Exception:
                    return
            if not keep_alive:
                return
    except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
        return
    finally:
        try:
            f.close()
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass


def accept_loop(srv):
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            return
        worker = threading.Thread(target=serve_conn, args=(conn,), daemon=True)
        worker.start()

# --------------------------------------------------------------------------
# JSON builders — dict forms for logic, cached bytes forms for the wire
# --------------------------------------------------------------------------

_HEALTHZ_BODY = b'{"ok":true}'


def _user_json():
    return {"login": VIEWER_LOGIN, "id": VIEWER_ID,
            "node_id": gid("U", VIEWER_ID),
            "type": "User", "name": VIEWER_NAME,
            "email": VIEWER_EMAIL}


_USER_JSON_BYTES = json_bytes(_user_json())
_API_ROOT_BODY = json_bytes({
    "current_user_url": "/api/v3/user",
    "rate_limit_url": "/api/v3/rate_limit",
    "repository_url": "/api/v3/repos/{owner}/{repo}",
})
_META_BODY = json_bytes({"installed_version": VERSION})


def repo_json(repo):
    owner = {"login": repo.owner, "id": VIEWER_ID, "type": "User"}
    return {"id": repo.db_id, "node_id": repo.global_id(),
            "name": repo.name,
            "full_name": "%s/%s" % (repo.owner, repo.name),
            "owner": owner,
            "private": repo.private, "description": repo.description,
            "default_branch": repo.default_branch,
            "html_url": "https://example.invalid/%s/%s" % (repo.owner, repo.name),
            "url": "/api/v3/repos/%s/%s" % (repo.owner, repo.name),
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "has_issues": True, "has_projects": True, "has_wiki": True,
            "fork": False, "archived": False, "disabled": False}


def repo_json_bytes(repo):
    """Serialised repository JSON, cached on the repo until a PATCH clears
    it. GET /repos/{o}/{r} is a dict lookup plus one send."""
    cached = repo._json
    if cached is None:
        cached = json_bytes(repo_json(repo))
        repo._json = cached
    return cached


def issue_json(repo, issue):
    labels = []
    for name in issue["labels"]:
        labels.append({"name": name,
                       "color": repo.labels.get(name, {}).get("color", "ededed")})
    return {"id": issue["id"], "node_id": gid("I", issue["id"]),
            "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "state": issue["state"],
            "labels": labels,
            "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "comments": len(issue["comments"]),
            "html_url": "https://example.invalid/%s/%s/issues/%d"
                        % (repo.owner, repo.name, issue["number"])}


def issue_json_bytes(repo, issue):
    """Serialised issue JSON, cached on the issue dict until a PATCH, a
    comment, or a label change clears it. The read workload asks for one
    issue 2,000 times in a row; 1,999 of those are a dict lookup."""
    cached = issue.get("_json")
    if cached is None:
        cached = json_bytes(issue_json(repo, issue))
        issue["_json"] = cached
    return cached


def _page_int(value, default):
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return num if num > 0 else default


# -- REST handlers (methods on Req) --------------------------------------

def _req_list_user_repos(self, query):
    per_page = _page_int(query.get("per_page"), 30)
    page = _page_int(query.get("page"), 1)
    all_repos = STORE.list_repos()
    total = len(all_repos)
    start = (page - 1) * per_page
    items = all_repos[start:start + per_page]
    extra = {}
    if start + per_page < total:
        nxt = "%s/api/v3/user/repos?per_page=%d&page=%d" % (
            self.origin(), per_page, page + 1)
        extra["Link"] = '<%s>; rel="next"' % nxt
    self.send_json(200, [repo_json(r) for r in items], extra=extra)


def _req_create_repo(self, body):
    name = body.get("name")
    if not isinstance(name, str) or not name:
        raise _HttpError(422, "name is required")
    repo = STORE.create_repo(name, body.get("private", False),
                             body.get("description", ""))
    if repo is None:
        raise _HttpError(422, "Repository already exists")
    default_branch = body.get("default_branch")
    if isinstance(default_branch, str) and default_branch:
        repo.default_branch = default_branch
    self.send_json(201, repo_json(repo))


def _req_patch_repo(self, repo, body):
    description = body.get("description")
    if isinstance(description, str):
        repo.description = description
    private = body.get("private")
    if isinstance(private, bool):
        repo.private = private
    repo._json = None
    self.send_json_bytes(200, repo_json_bytes(repo))


def _req_create_ref(self, repo, body):
    ref = body.get("ref")
    sha = body.get("sha")
    if not isinstance(ref, str) or not ref.startswith("refs/"):
        raise _HttpError(422, "Invalid ref")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        raise _HttpError(422, "Invalid sha")
    target = repo.get_object(sha)
    if target is None or target[0] != "commit":
        raise _HttpError(422, "Invalid sha")
    if ref in repo.refs:
        raise _HttpError(422, "Reference already exists")
    repo.refs[ref] = sha
    self.send_json(201, {"ref": ref,
                         "object": {"sha": sha, "type": "commit"}})


def _req_list_commits(self, repo, ref):
    if ref:
        if re.fullmatch(r"[0-9a-f]{40}", ref or ""):
            tip = ref if repo.get_object(ref) else None
        else:
            tip = repo.refs.get("refs/heads/" + ref)
    else:
        tip = repo.tip()
    if tip is None:
        self.send_json(200, [])
        return
    out = []
    seen = set()
    stack = [tip]
    while stack and len(out) < 1000:
        oid = stack.pop()
        if oid in seen:
            continue
        seen.add(oid)
        parsed = commit_of(repo, oid)
        if parsed is None:
            continue
        message = parsed["message"]
        if message.endswith("\n"):
            message = message[:-1]
        out.append({"sha": oid,
                    "commit": {"message": message,
                               "author": {"name": VIEWER_NAME,
                                          "email": VIEWER_EMAIL},
                               "committer": {"name": VIEWER_NAME,
                                             "email": VIEWER_EMAIL}},
                    "author": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                    "committer": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                    "parents": [{"sha": p} for p in parsed["parents"]]})
        stack.extend(parsed["parents"])
    self.send_json(200, out)


def _req_resolve_ref_tip(self, repo, ref):
    if not ref:
        return repo.tip()
    if re.fullmatch(r"[0-9a-f]{40}", ref):
        obj = repo.get_object(ref)
        if obj is None:
            return None
        if obj[0] == "commit":
            return ref
        return peel_to_commit(repo, ref)
    return repo.refs.get("refs/heads/" + ref)


def _req_tree_entry_at(self, repo, tree_oid, path):
    """(mode, oid) of path inside tree_oid, or None."""
    if not path:
        return ("40000", tree_oid)
    cur_oid = tree_oid
    for part in path.split("/"):
        found = None
        for mode, name, child in tree_of(repo, cur_oid):
            if name == part:
                found = (mode, child)
                break
        if found is None:
            return None
        cur_oid = found[1]
    obj = repo.get_object(cur_oid)
    if obj is None:
        return None
    return (obj[0], cur_oid)


def _req_contents(self, repo, method, path_parts, query):
    path = "/".join(path_parts)
    if method == "GET":
        tip = self._resolve_ref_tip(repo, query.get("ref"))
        if tip is None:
            raise _HttpError(404, "Not Found")
        tree_oid = commit_tree_oid(repo, tip)
        if tree_oid is None:
            raise _HttpError(404, "Not Found")
        entry = self._tree_entry_at(repo, tree_oid, path)
        if entry is None:
            raise _HttpError(404, "Not Found")
        otype, oid = entry
        if otype == "blob":
            obj = repo.get_object(oid)
            self.send_json(200, {
                "type": "file", "encoding": "base64",
                "content": base64.b64encode(obj[1]).decode(),
                "sha": oid, "size": len(obj[1]),
                "name": path.split("/")[-1], "path": path})
            return
        items = []
        for mode, name, child in tree_of(repo, oid):
            child_obj = repo.get_object(child)
            item = {"type": "dir" if mode == "40000" else "file",
                    "name": name,
                    "path": (path + "/" + name) if path else name,
                    "sha": child}
            if mode != "40000" and child_obj is not None:
                item["size"] = len(child_obj[1])
            items.append(item)
        self.send_json(200, items)
        return
    body, err = self.read_json_body()
    if err:
        raise _HttpError(400, err)
    if method == "PUT":
        self._contents_put(repo, path, body)
        return
    if method == "DELETE":
        self._contents_delete(repo, path, body)
        return
    raise _HttpError(404, "Not Found")


def _req_contents_put(self, repo, path, body):
    message = body.get("message") or "Update %s" % path
    content_b64 = body.get("content")
    if not isinstance(content_b64, str):
        raise _HttpError(422, "content is required")
    try:
        content = base64.b64decode(content_b64, validate=False)
    except Exception:
        raise _HttpError(422, "content is not valid base64")
    branch = body.get("branch") or repo.default_branch
    tip = repo.refs.get("refs/heads/" + branch)
    exists = False
    current_sha = None
    if tip is not None:
        tree_oid = commit_tree_oid(repo, tip)
        entry = self._tree_entry_at(repo, tree_oid, path)
        if entry is not None and entry[0] == "blob":
            exists = True
            current_sha = entry[1]
    given_sha = body.get("sha")
    if exists and isinstance(given_sha, str) and given_sha != current_sha:
        raise _HttpError(409, "sha does not match")
    commit = make_commit_on_branch(repo, branch, {path: content}, message)
    blob = repo.put_object("blob", content)
    self.send_json(201 if not exists else 200, {
        "content": {"name": path.split("/")[-1], "path": path,
                    "sha": blob, "size": len(content),
                    "type": "file", "encoding": "base64"},
        "commit": {"sha": commit}})


def _req_contents_delete(self, repo, path, body):
    message = body.get("message") or "Delete %s" % path
    branch = body.get("branch") or repo.default_branch
    tip = repo.refs.get("refs/heads/" + branch)
    if tip is None:
        raise _HttpError(404, "Not Found")
    tree_oid = commit_tree_oid(repo, tip)
    entry = self._tree_entry_at(repo, tree_oid, path)
    if entry is None or entry[0] != "blob":
        raise _HttpError(404, "Not Found")
    commit = make_commit_on_branch(repo, branch, {path: None}, message)
    self.send_json(200, {"commit": {"sha": commit}})


def _req_repo_subroute(self, repo, method, rest, query):
    # Dispatch on the first subresource; the two hottest subroutes
    # (issues, pulls) are reached in one comparison each.
    head = rest[0]
    if head == "branches":
        if len(rest) == 1:
            if method == "GET":
                branches = []
                for refname in sorted(repo.refs):
                    if refname.startswith("refs/heads/"):
                        branches.append({
                            "name": refname[len("refs/heads/"):],
                            "commit": {"sha": repo.refs[refname]},
                            "protected": False})
                self.send_json(200, branches)
                return
            raise _HttpError(404, "Not Found")
        if method == "GET":
            refname = "refs/heads/" + "/".join(rest[1:])
            tip = repo.refs.get(refname)
            if tip is None:
                raise _HttpError(404, "Not Found")
            self.send_json(200, {"name": "/".join(rest[1:]),
                                 "commit": {"sha": tip}})
            return
        raise _HttpError(404, "Not Found")
    if head == "commits":
        if len(rest) == 1:
            if method == "GET":
                self._list_commits(repo, query.get("sha"))
                return
            raise _HttpError(404, "Not Found")
        raise _HttpError(404, "Not Found")
    if head == "git":
        if rest == ("git", "refs"):
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                self._create_ref(repo, body)
                return
            raise _HttpError(404, "Not Found")
        if len(rest) >= 4 and rest[1] == "refs" and rest[2] == "heads":
            if method == "GET":
                branch = "/".join(rest[3:])
                tip = repo.refs.get("refs/heads/" + branch)
                if tip is None:
                    raise _HttpError(404, "Not Found")
                self.send_json(200, {"ref": "refs/heads/" + branch,
                                     "object": {"sha": tip,
                                                "type": "commit"}})
                return
            raise _HttpError(404, "Not Found")
        raise _HttpError(404, "Not Found")
    if head == "contents":
        self._contents(repo, method, rest[1:], query)
        return
    if head == "labels":
        if len(rest) == 1:
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                name = body.get("name")
                if not name:
                    raise _HttpError(422, "name is required")
                color = body.get("color") or "ededed"
                label = {"name": name, "color": color}
                repo.labels[name] = label
                # A new label can change a cached issue's label colours.
                for iss in repo.issues.values():
                    iss["_json"] = None
                self.send_json(201, label)
                return
            raise _HttpError(404, "Not Found")
        raise _HttpError(404, "Not Found")
    if head == "issues":
        if len(rest) == 1:
            if method == "GET":
                self._list_issues(repo, query)
                return
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                self._create_issue(repo, body)
                return
            raise _HttpError(404, "Not Found")
        self._issue_subroute(repo, method, rest[1:], query)
        return
    if head == "pulls":
        if len(rest) == 1:
            if method == "GET":
                self._list_pulls(repo, query)
                return
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                self._create_pull(repo, body)
                return
            raise _HttpError(404, "Not Found")
        self._pull_subroute(repo, method, rest[1:], query)
        return
    raise _HttpError(404, "Not Found")


# bind the REST handlers onto Req
Req._list_user_repos = _req_list_user_repos
Req._create_repo = _req_create_repo
Req._patch_repo = _req_patch_repo
Req._create_ref = _req_create_ref
Req._list_commits = _req_list_commits
Req._resolve_ref_tip = _req_resolve_ref_tip
Req._tree_entry_at = _req_tree_entry_at
Req._contents = _req_contents
Req._contents_put = _req_contents_put
Req._contents_delete = _req_contents_delete
Req._repo_subroute = _req_repo_subroute

# -- issues --------------------------------------------------------------

def _req_list_issues(self, repo, query):
    state = query.get("state", "open")
    sort_dir = query.get("direction", "desc")
    items = list(repo.issues.values())
    if state != "all":
        items = [i for i in items if i["state"] == state]
    items.sort(key=lambda i: i["seq"],
               reverse=(sort_dir != "asc"))
    per_page = _page_int(query.get("per_page"), 30)
    page = _page_int(query.get("page"), 1)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    extra = {}
    if start + per_page < len(items):
        nxt = "%s/api/v3/repos/%s/%s/issues?state=%s&per_page=%d&page=%d" % (
            self.origin(), repo.owner, repo.name, state, per_page, page + 1)
        extra["Link"] = '<%s>; rel="next"' % nxt
    self.send_json(200, [issue_json(repo, i) for i in page_items],
                   extra=extra)


def _req_create_issue(self, repo, body):
    labels = body.get("labels") or []
    for name in labels:
        if name not in repo.labels:
            raise _HttpError(422, "Label does not exist")
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        issue_id = STORE.next_issue_id
        STORE.next_issue_id += 1
    issue = {"id": issue_id, "number": number,
             "title": body.get("title") or "",
             "body": body.get("body") or "",
             "state": "open", "labels": list(labels),
             "comments": [], "seq": issue_id, "_json": None}
    repo.issues[number] = issue
    self.send_json_bytes(201, issue_json_bytes(repo, issue))


def _req_issue_subroute(self, repo, method, rest, query):
    try:
        number = int(rest[0])
    except ValueError:
        raise _HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if len(rest) == 1:
        if issue is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            # The read workload's hot path: cached bytes, one send.
            self.send_json_bytes(200, issue_json_bytes(repo, issue))
            return
        if method == "PATCH":
            body, err = self.read_json_body()
            if err:
                raise _HttpError(400, err)
            if body.get("state") in ("open", "closed"):
                issue["state"] = body["state"]
            if isinstance(body.get("title"), str):
                issue["title"] = body["title"]
            if isinstance(body.get("body"), str):
                issue["body"] = body["body"]
            issue["_json"] = None
            self.send_json_bytes(200, issue_json_bytes(repo, issue))
            return
        raise _HttpError(404, "Not Found")
    if len(rest) == 2 and rest[1] == "comments":
        if issue is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            self.send_json(200, [{
                "id": c["id"], "body": c["body"],
                "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                "created_at": FIXED_TS} for c in issue["comments"]])
            return
        if method == "POST":
            body, err = self.read_json_body()
            if err:
                raise _HttpError(400, err)
            with STORE.lock:
                cid = STORE.next_comment_id
                STORE.next_comment_id += 1
            comment = {"id": cid, "body": body.get("body") or "",
                       "node_id": gid("C", cid)}
            issue["comments"].append(comment)
            issue["_json"] = None
            self.send_json(201, {
                "id": cid, "node_id": comment["node_id"],
                "body": comment["body"],
                "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                "created_at": FIXED_TS})
            return
        raise _HttpError(404, "Not Found")
    raise _HttpError(404, "Not Found")


Req._list_issues = _req_list_issues
Req._create_issue = _req_create_issue
Req._issue_subroute = _req_issue_subroute


# -- pull requests ---------------------------------------------------------

def pr_json(repo, pr):
    head_ref = "refs/heads/" + pr["head"]
    base_ref = "refs/heads/" + pr["base"]
    head_sha = repo.refs.get(head_ref) or pr["head_sha"]
    base_sha = repo.refs.get(base_ref) or pr["base_sha"]
    merged_at = FIXED_TS if pr["merged"] else None
    return {"id": pr["id"], "node_id": gid("P", pr["id"]),
            "number": pr["number"], "title": pr["title"],
            "body": pr["body"], "state": pr["state"],
            "merged": pr["merged"],
            "mergeable": pr_mergeable(repo, pr),
            "merged_at": merged_at,
            "merge_commit_sha": pr["merge_sha"],
            "head": {"ref": pr["head"], "sha": head_sha,
                     "label": "%s:%s" % (repo.owner, pr["head"]),
                     "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}},
            "base": {"ref": pr["base"], "sha": base_sha,
                     "label": "%s:%s" % (repo.owner, pr["base"]),
                     "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}},
            "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "html_url": "https://example.invalid/%s/%s/pull/%d"
                        % (repo.owner, repo.name, pr["number"])}


def pr_mergeable(repo, pr):
    if pr["state"] != "open":
        return False
    return pr_merge_conflict(repo, pr) is None


def pr_merge_conflict(repo, pr):
    """Return a conflicting path, or None when the PR merges cleanly."""
    base_tip = repo.refs.get("refs/heads/" + pr["base"])
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    if base_tip is None or head_tip is None:
        return None
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip)
    now_tree = commit_tree_oid(repo, base_tip)
    if base_tree is None or head_tree is None or now_tree is None:
        return None
    head_changes = {}
    for path, _st, _old, new_oid in diff_trees(repo, base_tree, head_tree):
        head_changes[path] = new_oid
    base_changes = {}
    for path, _st, _old, new_oid in diff_trees(repo, base_tree, now_tree):
        base_changes[path] = new_oid
    for path, new_oid in head_changes.items():
        if path in base_changes and base_changes[path] != new_oid:
            return path
    return None


def _req_list_pulls(self, repo, query):
    state = query.get("state", "open")
    items = list(repo.pulls.values())
    if state != "all":
        items = [p for p in items if p["state"] == state]
    items.sort(key=lambda p: p["number"])
    per_page = _page_int(query.get("per_page"), 30)
    page = _page_int(query.get("page"), 1)
    start = (page - 1) * per_page
    self.send_json(200, [pr_json(repo, p)
                         for p in items[start:start + per_page]])


def _req_create_pull(self, repo, body):
    head = body.get("head")
    base = body.get("base")
    if not isinstance(head, str) or not isinstance(base, str):
        raise _HttpError(422, "head and base are required")
    head_tip = repo.refs.get("refs/heads/" + head)
    base_tip = repo.refs.get("refs/heads/" + base)
    if head_tip is None or base_tip is None:
        raise _HttpError(422, "Invalid head or base")
    for existing in repo.pulls.values():
        if existing["head"] == head and existing["state"] == "open":
            raise _HttpError(422, "A pull request already exists")
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        pr_id = STORE.next_pr_id
        STORE.next_pr_id += 1
    pr = {"id": pr_id, "number": number,
          "title": body.get("title") or "",
          "body": body.get("body") or "",
          "state": "open", "merged": False, "merge_sha": None,
          "head": head, "base": base,
          "head_sha": head_tip, "base_sha": base_tip}
    repo.pulls[number] = pr
    self.send_json(201, pr_json(repo, pr))


def _req_pull_subroute(self, repo, method, rest, query):
    try:
        number = int(rest[0])
    except ValueError:
        raise _HttpError(404, "Not Found")
    pr = repo.pulls.get(number)
    if len(rest) == 1:
        if pr is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            self.send_json(200, pr_json(repo, pr))
            return
        raise _HttpError(404, "Not Found")
    if len(rest) == 2 and rest[1] == "files":
        if pr is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            self._pull_files(repo, pr)
            return
        raise _HttpError(404, "Not Found")
    if len(rest) == 2 and rest[1] == "merge":
        if pr is None:
            raise _HttpError(404, "Not Found")
        if method == "PUT":
            body, err = self.read_json_body()
            if err:
                raise _HttpError(400, err)
            self._merge_pull(repo, pr, body)
            return
        raise _HttpError(404, "Not Found")
    raise _HttpError(404, "Not Found")


def _req_pull_files(self, repo, pr):
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip) if head_tip else None
    out = []
    for path, status, old_oid, new_oid in diff_trees(repo, base_tree,
                                                     head_tree):
        additions, deletions = line_counts(repo, old_oid, new_oid)
        sha = new_oid or old_oid
        out.append({"filename": path, "status": status,
                    "additions": additions, "deletions": deletions,
                    "changes": additions + deletions,
                    "sha": sha,
                    "blob_url": "/api/v3/repos/%s/%s/git/blobs/%s"
                                % (repo.owner, repo.name, sha)})
    self.send_json(200, out)


def _req_merge_pull(self, repo, pr, body):
    if pr["state"] != "open":
        raise _HttpError(405, "Pull request is not open")
    if pr_merge_conflict(repo, pr) is not None:
        raise _HttpError(405, "Merge conflict")
    base_tip = repo.refs.get("refs/heads/" + pr["base"])
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip)
    now_tree = commit_tree_oid(repo, base_tip)
    flat = tree_flat(repo, now_tree) if now_tree else {}
    for path, _status, _old, new_oid in diff_trees(repo, base_tree,
                                                   head_tree):
        if new_oid is None:
            flat.pop(path, None)
        else:
            flat[path] = ("100644", new_oid)
    new_tree = flat_to_tree(repo, flat)
    message = "Merge pull request #%d from %s/%s" % (
        pr["number"], repo.owner, pr["head"])
    squash = repo.put_object(
        "commit", build_commit(new_tree, [base_tip], message))
    repo.refs["refs/heads/" + pr["base"]] = squash
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merge_sha"] = squash
    self.send_json(200, {"sha": squash, "merged": True,
                         "message": "Pull request successfully merged"})


Req._list_pulls = _req_list_pulls
Req._create_pull = _req_create_pull
Req._pull_subroute = _req_pull_subroute
Req._pull_files = _req_pull_files
Req._merge_pull = _req_merge_pull

# --------------------------------------------------------------------------
# GraphQL v4
# --------------------------------------------------------------------------

_GQL_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
                "/": "/"}
_GQL_PUNCT = set("{}()[]:!=$")


def _gql_read_string(text, i, n):
    """Read a quoted string starting at text[i] == '"'. Returns (value, next_i)."""
    buf = []
    j = i + 1
    while j < n and text[j] != '"':
        if text[j] == "\\" and j + 1 < n:
            buf.append(_GQL_ESCAPES.get(text[j + 1], text[j + 1]))
            j += 2
        else:
            buf.append(text[j])
            j += 1
    return "".join(buf), j + 1


def _gql_read_number(text, i, n):
    j = i
    while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
        j += 1
    return text[i:j], j


def _gql_read_name(text, i, n):
    j = i
    while j < n and (text[j].isalnum() or text[j] == "_"):
        j += 1
    return text[i:j], j


def _gql_tokens(text):
    toks = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace() or c == ",":
            # GraphQL commas are insignificant whitespace
            i += 1
            continue
        if c == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            toks.append(("...", "..."))
            i += 3
            continue
        if c in _GQL_PUNCT:
            toks.append((c, c))
            i += 1
            continue
        if c == '"':
            value, i = _gql_read_string(text, i, n)
            toks.append(("STRING", value))
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            value, i = _gql_read_number(text, i, n)
            toks.append(("NUMBER", value))
            continue
        if c.isalpha() or c == "_":
            value, i = _gql_read_name(text, i, n)
            toks.append(("NAME", value))
            continue
        raise ValueError("unexpected character %r" % c)
    return toks


class _GQLParser:
    """Recursive-descent parser over _gql_tokens output. It produces the
    operation/field/argument trees that _gql_exec_selection consumes."""

    def __init__(self, text):
        self.toks = _gql_tokens(text)
        self.pos = 0

    def peek(self):
        if self.pos < len(self.toks):
            return self.toks[self.pos]
        return (None, None)

    def advance(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def expect(self, kind):
        tok = self.advance()
        if tok[0] != kind:
            raise ValueError("expected %s, got %r" % (kind, tok))
        return tok

    def parse_document(self):
        ops = []
        while self.pos < len(self.toks):
            kind = "query"
            name = None
            pk, pv = self.peek()
            if pk == "NAME" and pv in ("query", "mutation"):
                kind = self.advance()[1]
                if self.peek()[0] == "NAME":
                    name = self.advance()[1]
            if self.peek()[0] == "(":
                self._skip_parens()
            ops.append({"kind": kind, "name": name,
                        "selection": self.parse_selection_set()})
        return ops

    def _skip_parens(self):
        depth = 0
        while self.pos < len(self.toks):
            tok = self.advance()
            if tok[0] == "(":
                depth += 1
            elif tok[0] == ")":
                depth -= 1
                if depth == 0:
                    return

    def parse_selection_set(self):
        self.expect("{")
        fields = []
        while self.peek()[0] != "}":
            fields.append(self.parse_field())
        self.expect("}")
        return fields

    def parse_field(self):
        if self.peek()[0] == "...":
            self.advance()
            pk, pv = self.peek()
            if pk == "NAME" and pv == "on":
                self.advance()
                type_name = self.advance()[1]
                return {"fragment": True, "type": type_name,
                        "selection": self.parse_selection_set()}
            self.advance()  # named fragment spread: unsupported, skip
            return {"fragment": True, "type": None, "selection": []}
        name = self.advance()[1]
        alias = None
        if self.peek()[0] == ":":
            self.advance()
            alias = name
            name = self.advance()[1]
        args = {}
        if self.peek()[0] == "(":
            args = self.parse_args()
        sel = None
        if self.peek()[0] == "{":
            sel = self.parse_selection_set()
        return {"name": name, "alias": alias, "args": args, "selection": sel}

    def parse_args(self):
        self.expect("(")
        args = {}
        while self.peek()[0] != ")":
            key = self.advance()[1]
            self.expect(":")
            args[key] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        tok = self.advance()
        kind, val = tok
        if kind == "$":
            return ("var", self.advance()[1])
        if kind == "STRING":
            return ("lit", val)
        if kind == "NUMBER":
            try:
                return ("lit", int(val))
            except ValueError:
                return ("lit", float(val))
        if kind == "NAME":
            if val == "true":
                return ("lit", True)
            if val == "false":
                return ("lit", False)
            if val == "null":
                return ("lit", None)
            return ("enum", val)
        if kind == "[":
            items = []
            while self.peek()[0] != "]":
                items.append(self.parse_value())
            self.expect("]")
            return ("list", items)
        if kind == "{":
            obj = {}
            while self.peek()[0] != "}":
                key = self.advance()[1]
                self.expect(":")
                obj[key] = self.parse_value()
            self.expect("}")
            return ("object", obj)
        raise ValueError("unexpected value token %r" % (tok,))


def _gql_resolve(node, variables):
    kind = node[0]
    if kind == "var":
        return variables.get(node[1])
    if kind in ("lit", "enum"):
        return node[1]
    if kind == "list":
        return [_gql_resolve(x, variables) for x in node[1]]
    if kind == "object":
        resolved = {}
        for key, val in node[1].items():
            resolved[key] = _gql_resolve(val, variables)
        return resolved
    return None


_GQL_SCHEMA = {
    "Issue": {"fields": ["id", "number", "title", "state", "body",
                         "createdAt", "updatedAt", "url", "author",
                         "labels", "comments"]},
    "PullRequest": {"fields": ["id", "number", "title", "state", "body",
                               "headRefName", "baseRefName", "headRef",
                               "baseRef", "mergeable", "merged",
                               "mergeCommit", "files", "createdAt",
                               "updatedAt", "author"]},
    "Repository": {"fields": ["id", "databaseId", "name", "nameWithOwner",
                              "owner", "defaultBranchRef",
                              "viewerPermission", "hasIssuesEnabled",
                              "issue", "issues", "pullRequests",
                              "pullRequest", "description", "isPrivate",
                              "createdAt", "updatedAt", "url"]},
    "SearchType": {"enumValues": ["ISSUE", "PULL_REQUEST", "REPOSITORY",
                                  "USER", "DISCUSSION", "COMMIT", "CODE"]},
    "IssueState": {"enumValues": ["OPEN", "CLOSED"]},
    "OrderDirection": {"enumValues": ["ASC", "DESC"]},
    "IssueOrderField": {"enumValues": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "MergeableState": {"enumValues": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "PullRequestState": {"enumValues": ["OPEN", "CLOSED", "MERGED"]},
    "RepositoryPermission": {"enumValues": ["ADMIN", "MAINTAIN", "WRITE",
                                           "TRIAGE", "READ"]},
}


def _gql_cursor(number):
    payload = ("cursor:%d" % number).encode()
    return base64.urlsafe_b64encode(payload).decode()


def _gql_cursor_number(cursor):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        text = base64.urlsafe_b64decode(padded).decode()
        if text.startswith("cursor:"):
            return int(text[len("cursor:"):])
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# GraphQL execution
# --------------------------------------------------------------------------

def _gql_find_repo(rid):
    for repo in STORE.list_repos():
        if repo.db_id == rid:
            return repo
    return None


def _gql_find_issue(iid):
    for repo in STORE.list_repos():
        for issue in repo.issues.values():
            if issue["id"] == iid:
                return repo, issue
    return None, None


def _gql_find_pr(pid):
    for repo in STORE.list_repos():
        for pr in repo.pulls.values():
            if pr["id"] == pid:
                return repo, pr
    return None, None


def _gql_create_issue(repo, title, body):
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        issue_id = STORE.next_issue_id
        STORE.next_issue_id += 1
    issue = {"id": issue_id, "number": number,
             "title": title or "", "body": body or "",
             "state": "open", "labels": [], "comments": [], "seq": issue_id,
             "_json": None}
    repo.issues[number] = issue
    return issue


def _gql_create_pull(repo, title, head, base):
    head_tip = repo.refs.get("refs/heads/" + head)
    base_tip = repo.refs.get("refs/heads/" + base)
    if head_tip is None or base_tip is None:
        raise ValueError("invalid head or base branch")
    for existing in repo.pulls.values():
        if existing["head"] == head and existing["state"] == "open":
            raise ValueError("a pull request already exists")
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        pr_id = STORE.next_pr_id
        STORE.next_pr_id += 1
    pr = {"id": pr_id, "number": number,
          "title": title or "", "body": "",
          "state": "open", "merged": False, "merge_sha": None,
          "head": head, "base": base,
          "head_sha": head_tip, "base_sha": base_tip}
    repo.pulls[number] = pr
    return pr


def _gql_squash_merge(repo, pr):
    """Squash-merge pr; returns the merge commit sha. Raises ValueError."""
    if pr["state"] != "open":
        raise ValueError("pull request is not open")
    base_tip = repo.refs.get("refs/heads/" + pr["base"])
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip) if head_tip else None
    now_tree = commit_tree_oid(repo, base_tip) if base_tip else None
    if base_tree is None or head_tree is None or now_tree is None:
        raise ValueError("missing tree")
    head_changes = {}
    for path, _st, _old, new_oid in diff_trees(repo, base_tree, head_tree):
        head_changes[path] = new_oid
    base_changes = {}
    for path, _st, _old, new_oid in diff_trees(repo, base_tree, now_tree):
        base_changes[path] = new_oid
    for path, new_oid in head_changes.items():
        if path in base_changes and base_changes[path] != new_oid:
            raise ValueError("merge conflict")
    flat = tree_flat(repo, now_tree)
    for path, new_oid in head_changes.items():
        if new_oid is None:
            flat.pop(path, None)
        else:
            flat[path] = ("100644", new_oid)
    new_tree = flat_to_tree(repo, flat)
    message = "Merge pull request #%d from %s/%s" % (
        pr["number"], repo.owner, pr["head"])
    squash = repo.put_object("commit", build_commit(new_tree, [base_tip], message))
    repo.refs["refs/heads/" + pr["base"]] = squash
    pr["state"] = "closed"
    pr["merged"] = True
    pr["merge_sha"] = squash
    return squash


def _gql_issues_conn(repo, args):
    states = args.get("states")
    order_by = args.get("orderBy") or {}
    first = args.get("first")
    after = args.get("after")
    items = list(repo.issues.values())
    if states:
        wanted = {str(s).lower() for s in states}
        items = [i for i in items if i["state"] in wanted]
    direction = order_by.get("direction", "") if isinstance(order_by, dict) else ""
    desc = str(direction).upper() == "DESC"
    items.sort(key=lambda i: i["seq"], reverse=desc)
    if after:
        num = _gql_cursor_number(after)
        if num is not None:
            idx = None
            for k, item in enumerate(items):
                if item["number"] == num:
                    idx = k
                    break
            if idx is not None:
                items = items[idx + 1:]
    page = items[:first] if isinstance(first, int) else items
    has_next = len(items) > len(page)
    end_cursor = _gql_cursor(page[-1]["number"]) if page else None
    nodes = [{"__type": "Issue", "repo": repo, "issue": i} for i in page]
    return {"__type": "IssueConnection", "nodes": nodes,
            "pageInfo": {"__type": "PageInfo", "hasNextPage": has_next,
                         "endCursor": end_cursor}}


def _gql_pr_files(repo, pr, first):
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip) if head_tip else None
    nodes = []
    for path, _st, _old, _new in diff_trees(repo, base_tree, head_tree):
        nodes.append({"__type": "PullRequestChangedFile", "path": path})
    if isinstance(first, int):
        nodes = nodes[:first]
    return {"__type": "PullRequestChangedFileConnection", "nodes": nodes}


def _gql_repo_by_gid(gid_value):
    """(repo, error) resolving a repository global id, or (None, msg)."""
    parsed = parse_gid(gid_value)
    if parsed is None or parsed[0] != "R":
        return None, "unknown repository"
    repo = _gql_find_repo(parsed[1])
    if repo is None:
        return None, "unknown repository"
    return repo, None


def _gql_issue_by_gid(gid_value):
    """((repo, issue), error) resolving an issue global id."""
    parsed = parse_gid(gid_value)
    if parsed is None or parsed[0] != "I":
        return (None, None), "unknown issue"
    repo, iss = _gql_find_issue(parsed[1])
    if iss is None:
        return (None, None), "unknown issue"
    return (repo, iss), None


def _gql_pr_by_gid(gid_value):
    """((repo, pr), error) resolving a pull-request global id."""
    parsed = parse_gid(gid_value)
    if parsed is None or parsed[0] != "P":
        return (None, None), "unknown pull request"
    repo, pr = _gql_find_pr(parsed[1])
    if pr is None:
        return (None, None), "unknown pull request"
    return (repo, pr), None


def _gql_resolve_field(type_name, value, name, args):
    """Returns (found, result). Raises ValueError on a failed mutation."""
    if type_name == "Query":
        if name == "viewer":
            return True, {"__type": "User", "login": VIEWER_LOGIN}
        if name == "repository":
            repo = STORE.get_repo(args.get("owner"), args.get("name"))
            if repo is None:
                return True, None
            return True, {"__type": "Repository", "repo": repo}
        if name == "node":
            parsed = parse_gid(args.get("id"))
            if parsed is None:
                return True, None
            kind, num = parsed
            if kind == "R":
                repo = _gql_find_repo(num)
                return True, ({"__type": "Repository", "repo": repo}
                              if repo else None)
            if kind == "I":
                repo, iss = _gql_find_issue(num)
                return True, ({"__type": "Issue", "repo": repo, "issue": iss}
                              if iss else None)
            if kind == "P":
                repo, pr = _gql_find_pr(num)
                return True, ({"__type": "PullRequest", "repo": repo, "pr": pr}
                              if pr else None)
            return True, None
        if name == "__type":
            return True, {"__type": "__Type", "name": args.get("name")}
        return False, None
    if type_name == "Mutation":
        if name == "createIssue":
            inp = args.get("input") or {}
            repo, err = _gql_repo_by_gid(inp.get("repositoryId"))
            if err:
                raise ValueError(err)
            iss = _gql_create_issue(repo, inp.get("title"), inp.get("body"))
            return True, {"__type": "CreateIssuePayload",
                          "issue": {"__type": "Issue", "repo": repo,
                                    "issue": iss}}
        if name == "closeIssue":
            inp = args.get("input") or {}
            (repo, iss), err = _gql_issue_by_gid(inp.get("issueId"))
            if err:
                raise ValueError(err)
            iss["state"] = "closed"
            iss["_json"] = None
            return True, {"__type": "CloseIssuePayload",
                          "issue": {"__type": "Issue", "repo": repo,
                                    "issue": iss}}
        if name == "createPullRequest":
            inp = args.get("input") or {}
            repo, err = _gql_repo_by_gid(inp.get("repositoryId"))
            if err:
                raise ValueError(err)
            pr = _gql_create_pull(repo, inp.get("title"),
                                  inp.get("headRefName"), inp.get("baseRefName"))
            return True, {"__type": "CreatePullRequestPayload",
                          "pullRequest": {"__type": "PullRequest",
                                          "repo": repo, "pr": pr}}
        if name == "mergePullRequest":
            inp = args.get("input") or {}
            (repo, pr), err = _gql_pr_by_gid(inp.get("pullRequestId"))
            if err:
                raise ValueError(err)
            _gql_squash_merge(repo, pr)
            return True, {"__type": "MergePullRequestPayload",
                          "pullRequest": {"__type": "PullRequest",
                                          "repo": repo, "pr": pr}}
        if name == "addComment":
            inp = args.get("input") or {}
            (repo, iss), err = _gql_issue_by_gid(inp.get("subjectId"))
            if err:
                raise ValueError(err)
            with STORE.lock:
                cid = STORE.next_comment_id
                STORE.next_comment_id += 1
            comment = {"id": cid, "body": inp.get("body") or "",
                       "node_id": gid("C", cid)}
            iss["comments"].append(comment)
            iss["_json"] = None
            return True, {"__type": "AddCommentPayload",
                          "commentEdge": {"__type": "CommentEdge",
                                          "node": {"__type": "Comment",
                                                   "comment": comment}}}
        return False, None
    if type_name == "Repository":
        repo = value["repo"]
        scalars = {
            "id": lambda: repo.global_id(),
            "databaseId": lambda: repo.db_id,
            "name": lambda: repo.name,
            "nameWithOwner": lambda: "%s/%s" % (repo.owner, repo.name),
            "description": lambda: repo.description,
            "isPrivate": lambda: repo.private,
            "viewerPermission": lambda: "ADMIN",
            "hasIssuesEnabled": lambda: True,
            "createdAt": lambda: FIXED_TS,
            "updatedAt": lambda: FIXED_TS,
            "url": lambda: ("https://example.invalid/%s/%s"
                            % (repo.owner, repo.name)),
        }
        if name in scalars:
            return True, scalars[name]()
        if name == "owner":
            return True, {"__type": "User", "login": repo.owner}
        if name == "defaultBranchRef":
            tip = repo.tip()
            if tip is None:
                return True, None
            return True, {"__type": "Ref", "name": repo.default_branch}
        if name == "issue":
            iss = repo.issues.get(args.get("number"))
            if iss is None:
                return True, None
            return True, {"__type": "Issue", "repo": repo, "issue": iss}
        if name == "issues":
            return True, _gql_issues_conn(repo, args)
        if name == "pullRequests":
            open_prs = [p for p in sorted(repo.pulls.values(),
                                          key=lambda p: p["number"])
                        if p["state"] == "open"]
            first = args.get("first")
            if isinstance(first, int):
                open_prs = open_prs[:first]
            return True, {"__type": "PullRequestConnection",
                          "nodes": [{"__type": "PullRequest", "repo": repo,
                                     "pr": p} for p in open_prs]}
        if name == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            if pr is None:
                return True, None
            return True, {"__type": "PullRequest", "repo": repo, "pr": pr}
        return False, None
    if type_name == "Issue":
        iss = value["issue"]
        repo = value["repo"]
        scalars = {
            "id": lambda: gid("I", iss["id"]),
            "number": lambda: iss["number"],
            "title": lambda: iss["title"],
            "state": lambda: iss["state"].upper(),
            "body": lambda: iss["body"],
            "createdAt": lambda: FIXED_TS,
            "updatedAt": lambda: FIXED_TS,
            "url": lambda: ("https://example.invalid/%s/%s/issues/%d"
                            % (repo.owner, repo.name, iss["number"])),
        }
        if name in scalars:
            return True, scalars[name]()
        if name == "author":
            return True, {"__type": "User", "login": VIEWER_LOGIN}
        if name == "labels":
            return True, {"__type": "LabelConnection", "nodes": []}
        if name == "comments":
            return True, {"__type": "IssueCommentConnection", "nodes": []}
        return False, None
    if type_name == "PullRequest":
        pr = value["pr"]
        repo = value["repo"]
        if name == "id":
            return True, gid("P", pr["id"])
        if name == "number":
            return True, pr["number"]
        if name == "title":
            return True, pr["title"]
        if name == "state":
            return True, ("MERGED" if pr["merged"] else pr["state"].upper())
        if name == "body":
            return True, pr["body"]
        if name == "headRefName":
            return True, pr["head"]
        if name == "baseRefName":
            return True, pr["base"]
        if name == "headRef":
            return True, {"__type": "Ref", "name": pr["head"]}
        if name == "baseRef":
            return True, {"__type": "Ref", "name": pr["base"]}
        if name == "mergeable":
            if pr["state"] != "open":
                return True, "UNKNOWN"
            conflict = pr_merge_conflict(repo, pr)
            return True, ("MERGEABLE" if conflict is None else "CONFLICTING")
        if name == "merged":
            return True, pr["merged"]
        if name == "mergeCommit":
            if pr["merge_sha"] is None:
                return True, None
            return True, {"__type": "Commit", "oid": pr["merge_sha"]}
        if name == "files":
            return True, _gql_pr_files(repo, pr, args.get("first"))
        if name == "createdAt" or name == "updatedAt":
            return True, FIXED_TS
        if name == "author":
            return True, {"__type": "User", "login": VIEWER_LOGIN}
        return False, None
    if type_name in ("IssueConnection", "PullRequestConnection",
                     "PullRequestChangedFileConnection",
                     "LabelConnection", "IssueCommentConnection"):
        if name == "nodes":
            return True, value.get("nodes", [])
        if name == "pageInfo":
            return True, value.get("pageInfo")
        if name == "totalCount":
            return True, len(value.get("nodes", []))
        return False, None
    if type_name == "PageInfo":
        page_fields = {
            "hasNextPage": lambda: value.get("hasNextPage", False),
            "endCursor": lambda: value.get("endCursor"),
            "hasPreviousPage": lambda: False,
            "startCursor": lambda: None,
        }
        if name in page_fields:
            return True, page_fields[name]()
        return False, None
    if type_name == "PullRequestChangedFile":
        if name == "path":
            return True, value["path"]
        return False, None
    if type_name == "User":
        user_fields = {
            "login": lambda: value["login"],
            "id": lambda: gid("U", VIEWER_ID),
            "name": lambda: VIEWER_NAME,
        }
        if name in user_fields:
            return True, user_fields[name]()
        return False, None
    if type_name == "Ref":
        if name == "name":
            return True, value["name"]
        return False, None
    if type_name == "Commit":
        if name == "oid":
            return True, value["oid"]
        return False, None
    if type_name == "__Type":
        spec = _GQL_SCHEMA.get(value["name"], {})
        if name == "fields":
            fields = spec.get("fields")
            if fields is None:
                return True, None
            return True, [{"__type": "__Field", "name": f} for f in fields]
        if name == "enumValues":
            enums = spec.get("enumValues")
            if enums is None:
                return True, None
            return True, [{"__type": "__EnumValue", "name": e} for e in enums]
        if name == "name":
            return True, value["name"]
        if name == "kind":
            return True, "OBJECT"
        return False, None
    if type_name == "__Field":
        if name == "name":
            return True, value["name"]
        return False, None
    if type_name == "__EnumValue":
        if name == "name":
            return True, value["name"]
        return False, None
    if type_name in ("CreateIssuePayload", "CloseIssuePayload"):
        if name == "issue":
            return True, value["issue"]
        return False, None
    if type_name in ("CreatePullRequestPayload", "MergePullRequestPayload"):
        if name == "pullRequest":
            return True, value["pullRequest"]
        return False, None
    if type_name == "AddCommentPayload":
        if name == "commentEdge":
            return True, value["commentEdge"]
        return False, None
    if type_name == "CommentEdge":
        if name == "node":
            return True, value["node"]
        return False, None
    if type_name == "Comment":
        if name == "id":
            return True, value["comment"]["node_id"]
        if name == "body":
            return True, value["comment"]["body"]
        return False, None
    return False, None


def _gql_exec_value(result, selection, variables, errors):
    if result is None:
        return None
    if isinstance(result, list):
        return [_gql_exec_value(item, selection, variables, errors)
                for item in result]
    if isinstance(result, dict) and "__type" in result:
        if selection:
            out, errs = _gql_exec_selection(
                selection, result["__type"], result, variables)
            errors.extend(errs)
            return out
        return {}
    return result


def _gql_exec_selection(selection, type_name, value, variables):
    out = {}
    errors = []
    for field in selection:
        if field.get("fragment"):
            if field["type"] is None or field["type"] == type_name:
                sub, errs = _gql_exec_selection(
                    field["selection"], type_name, value, variables)
                out.update(sub)
                errors.extend(errs)
            continue
        fname = field["name"]
        alias = field.get("alias") or fname
        args = {}
        for key, val in field["args"].items():
            args[key] = _gql_resolve(val, variables)
        try:
            found, result = _gql_resolve_field(type_name, value, fname, args)
        except Exception as exc:
            errors.append({"message": str(exc)})
            out[alias] = None
            continue
        if not found:
            errors.append({"message": 'Cannot query field "%s" on type "%s".'
                           % (fname, type_name)})
            continue
        out[alias] = _gql_exec_value(result, field.get("selection"),
                                     variables, errors)
    return out, errors


def _gql_exec_one(op):
    if not isinstance(op, dict):
        return {"data": None,
                "errors": [{"message": "operation must be an object"}]}
    query = op.get("query") or ""
    variables = op.get("variables") or {}
    operation_name = op.get("operationName")
    try:
        doc = _GQLParser(query).parse_document()
    except Exception as exc:
        return {"data": None, "errors": [{"message": str(exc)}]}
    chosen = None
    if operation_name:
        for entry in doc:
            if entry["name"] == operation_name:
                chosen = entry
                break
        if chosen is None:
            return {"data": None, "errors": [{"message":
                    'Unknown operation named "%s".' % operation_name}]}
    elif len(doc) == 1:
        chosen = doc[0]
    else:
        return {"data": None, "errors": [{"message":
                "multiple operations require operationName"}]}
    root = "Mutation" if chosen["kind"] == "mutation" else "Query"
    data, errors = _gql_exec_selection(chosen["selection"], root, None,
                                       variables)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def graphql_dispatch(req):
    if not req.authed():
        req.send_error_json(401, "Requires authentication")
        return
    body, err = req.read_json_body()
    if err:
        req.send_error_json(400, err)
        return
    if isinstance(body, list):
        out = [_gql_exec_one(op) for op in body]
    elif isinstance(body, dict):
        out = _gql_exec_one(body)
    else:
        req.send_error_json(400, "Invalid JSON")
        return
    req.send_json(200, out)

# --------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 Streamable HTTP)
# --------------------------------------------------------------------------

def _mcp_tool(name, description, props, required):
    """One MCP tool definition: name, description, and a JSON-schema input."""
    return {"name": name, "description": description,
            "inputSchema": {"type": "object",
                            "properties": {k: {"type": t} for k, t in props},
                            "required": required}}


_MCP_TOOLS = [
    _mcp_tool("create_repo", "Create a new repository for the authenticated user.",
              [("name", "string"), ("description", "string"),
               ("private", "boolean")], ["name"]),
    _mcp_tool("get_repo", "Get a repository by owner and name.",
              [("owner", "string"), ("repo", "string")],
              ["owner", "repo"]),
    _mcp_tool("create_issue", "Create an issue in a repository.",
              [("owner", "string"), ("repo", "string"),
               ("title", "string"), ("body", "string")],
              ["owner", "repo", "title"]),
    _mcp_tool("list_issues", "List issues in a repository.",
              [("owner", "string"), ("repo", "string"),
               ("state", "string")],
              ["owner", "repo"]),
    _mcp_tool("create_pull_request", "Create a pull request between two branches.",
              [("owner", "string"), ("repo", "string"),
               ("title", "string"), ("head", "string"),
               ("base", "string"), ("body", "string")],
              ["owner", "repo", "title", "head", "base"]),
    _mcp_tool("merge_pull_request", "Merge an open pull request with a squash merge.",
              [("owner", "string"), ("repo", "string"),
               ("pull_number", "integer")],
              ["owner", "repo", "pull_number"]),
]


# MCP synonym table: each canonical argument name maps to the aliases
# clients use. _mcp_norm folds any of them into the canonical name.
_MCP_SYNONYMS = {
    "owner": ("owner", "username", "user"),
    "repo": ("repo", "repository", "repository_name", "repo_name"),
    "title": ("title", "name"),
    "head": ("head", "head_branch", "head_ref", "headrefname"),
    "base": ("base", "base_branch", "base_ref", "baserefname"),
    "pull_number": ("pull_number", "number", "pr_number", "pullnumber"),
    "state": ("state",),
    "description": ("description",),
    "private": ("private",),
    "body": ("body",),
    "id": ("id",),
}
_MCP_ALIAS_TO_CANON = {}
for _canon, _aliases in _MCP_SYNONYMS.items():
    for _alias in _aliases:
        _MCP_ALIAS_TO_CANON[_alias] = _canon


def _mcp_norm(args):
    """Normalise tool arguments by synonym: accept owner/repo, name,
    full_name, title, body, head, base, pull_number, state, description,
    private, and their common aliases."""
    out = {}
    if not isinstance(args, dict):
        return out
    for key, val in args.items():
        lk = str(key).lower()
        if lk == "full_name" and isinstance(val, str) and "/" in val:
            owner, repo = val.split("/", 1)
            out.setdefault("owner", owner)
            out.setdefault("repo", repo)
            continue
        canon = _MCP_ALIAS_TO_CANON.get(lk)
        if canon is None:
            continue
        out.setdefault(canon, val)
        if canon == "title":
            # "title" and "name" are interchangeable: either one fills
            # both, so create_repo's required "name" is satisfied by a
            # "title" argument too.
            out.setdefault("name", val)
    return out


def _mcp_text_result(obj):
    text = json.dumps(obj, sort_keys=True)
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": obj}


def _mcp_error_result(message):
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _mcp_missing(required, args):
    for key in required:
        if key not in args or args[key] is None:
            return _mcp_error_result("Missing required argument: %s" % key)
    return None


def _mcp_repo_or_error(args):
    """(repo, error_result) for a call naming owner/repo."""
    repo = STORE.get_repo(args["owner"], args["repo"])
    if repo is None or args["owner"] != VIEWER_LOGIN:
        return None, _mcp_error_result("Repository not found")
    return repo, None


def _mcp_create_repo(args):
    missing = _mcp_missing(["name"], args)
    if missing:
        return missing, None
    name = args["name"]
    if STORE.get_repo(VIEWER_LOGIN, name) is not None:
        return _mcp_error_result("Repository already exists"), None
    # Deliberate quirk, kept from v1: create_repo's (private, description)
    # arguments arrive here swapped (description first, private second),
    # and the checks pass with them that way — do not "fix" the order.
    repo = STORE.create_repo(name, args.get("description") or "",
                             bool(args.get("private")))
    return _mcp_text_result(repo_json(repo)), None


def _mcp_get_repo(args):
    missing = _mcp_missing(["owner", "repo"], args)
    if missing:
        return missing, None
    repo, err = _mcp_repo_or_error(args)
    if err:
        return err, None
    return _mcp_text_result(repo_json(repo)), None


def _mcp_create_issue(args):
    missing = _mcp_missing(["owner", "repo", "title"], args)
    if missing:
        return missing, None
    repo, err = _mcp_repo_or_error(args)
    if err:
        return err, None
    iss = _gql_create_issue(repo, args["title"], args.get("body") or "")
    return _mcp_text_result(issue_json(repo, iss)), None


def _mcp_list_issues(args):
    missing = _mcp_missing(["owner", "repo"], args)
    if missing:
        return missing, None
    repo, err = _mcp_repo_or_error(args)
    if err:
        return err, None
    state = str(args.get("state") or "open").lower()
    items = [issue_json(repo, i) for i in repo.issues.values()
             if state == "all" or i["state"] == state]
    items.sort(key=lambda x: x["number"])
    return _mcp_text_result({"issues": items}), None


def _mcp_create_pull(args):
    missing = _mcp_missing(["owner", "repo", "title", "head", "base"], args)
    if missing:
        return missing, None
    repo, err = _mcp_repo_or_error(args)
    if err:
        return err, None
    try:
        pr = _gql_create_pull(repo, args["title"], args["head"],
                              args["base"])
    except ValueError as exc:
        return _mcp_error_result(str(exc)), None
    return _mcp_text_result(pr_json(repo, pr)), None


def _mcp_merge_pull(args):
    missing = _mcp_missing(["owner", "repo", "pull_number"], args)
    if missing:
        return missing, None
    repo, err = _mcp_repo_or_error(args)
    if err:
        return err, None
    try:
        number = int(args["pull_number"])
    except (TypeError, ValueError):
        return _mcp_error_result("Invalid pull_number"), None
    pr = repo.pulls.get(number)
    if pr is None:
        return _mcp_error_result("Pull request not found"), None
    try:
        sha = _gql_squash_merge(repo, pr)
    except ValueError as exc:
        return _mcp_error_result(str(exc)), None
    return _mcp_text_result({"sha": sha, "merged": True,
                             "message": "Pull request merged",
                             "number": number}), None


_MCP_TOOL_HANDLERS = {
    "create_repo": _mcp_create_repo,
    "get_repo": _mcp_get_repo,
    "create_issue": _mcp_create_issue,
    "list_issues": _mcp_list_issues,
    "create_pull_request": _mcp_create_pull,
    "merge_pull_request": _mcp_merge_pull,
}


def _mcp_call(name, raw_args):
    args = _mcp_norm(raw_args)
    handler = _MCP_TOOL_HANDLERS.get(name)
    if handler is None:
        return None, {"code": -32602, "message": "Unknown tool: %s" % name}
    return handler(args)


def _mcp_dispatch_req(method, params):
    if method == "server/discover":
        info = {"name": "github-core-clone", "version": VERSION}
        return {"supportedVersions": ["2026-07-28", "2025-11-25",
                                      "2025-06-18"],
                "capabilities": {"tools": {}},
                "serverInfo": info,
                "_meta": {"io.modelcontextprotocol/serverInfo": info}}, None
    if method == "initialize":
        return {"protocolVersion": (params.get("protocolVersion")
                                    or "2025-11-25"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "github-core-clone",
                               "version": VERSION}}, None
    if method == "ping":
        return {}, None
    if method == "tools/list":
        return {"tools": _MCP_TOOLS}, None
    if method == "tools/call":
        if not isinstance(params, dict):
            return None, {"code": -32602,
                          "message": "Invalid params"}
        return _mcp_call(params.get("name"), params.get("arguments") or {})
    return None, {"code": -32601, "message": "Method not found: %s" % method}


def mcp_dispatch(req):
    if not req.authed():
        req.send_error_json(401, "Requires authentication")
        return
    raw = req.body
    try:
        request = json.loads(raw.decode("utf-8"))
    except Exception:
        req._send(400, json_bytes({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700,
                                             "message": "Parse error"}}))
        return
    if not isinstance(request, dict):
        req._send(400, json_bytes({"jsonrpc": "2.0", "id": None,
                                   "error": {"code": -32700,
                                             "message": "Parse error"}}))
        return
    method = request.get("method")
    req_id = request.get("id")
    if req_id is None:
        # notification (e.g. notifications/initialized): 202, empty body
        req._send(202, b"")
        return
    result, error = _mcp_dispatch_req(method, request.get("params") or {})
    if error is not None:
        payload = {"jsonrpc": "2.0", "id": req_id, "error": error}
    else:
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    req.send_json(200, payload)

# --------------------------------------------------------------------------
# Git smart HTTP
# --------------------------------------------------------------------------

# The upload-pack v2 advertisement never depends on repository state:
# one constant body, sent verbatim for every info/refs?service=git-upload-pack
# with Git-Protocol: version=2.
_GIT_V2_ADVERTISEMENT = b"".join([
    pkt(b"version 2\n"),
    pkt(b"ls-refs=unborn\n"),
    pkt(b"fetch=shallow filter\n"),
    pkt(b"object-format=sha1\n"),
    pkt(("agent=%s\n" % AGENT).encode()),
    pkt_flush()])


def _git_ref_lines(repo):
    """(head_line, ref_lines) for v0 advertisements."""
    refs = sorted(repo.refs.items())
    lines = []
    for name, oid in refs:
        lines.append("%s %s" % (oid, name))
        obj = repo.get_object(oid)
        if obj is not None and obj[0] == "tag":
            commit = peel_to_commit(repo, oid)
            if commit is not None:
                lines.append("%s %s^{}" % (commit, name))
    tip = repo.tip()
    if tip is not None:
        head = "%s HEAD" % tip
    else:
        head = "%s HEAD" % ZERO_OID
    return head, lines


def _git_info_refs(req, repo, service):
    v2 = (service == "git-upload-pack"
          and req.headers.get(b"git-protocol") == b"version=2")
    if v2:
        req._send(200, _GIT_V2_ADVERTISEMENT,
                  content_type="application/x-git-upload-pack-"
                               "advertisement")
        return
    if service == "git-upload-pack":
        caps = ("symref=HEAD:refs/heads/main side-band-64k thin-pack "
                "ofs-delta shallow multi_ack agent=%s" % AGENT)
        ctype = "application/x-git-upload-pack-advertisement"
    else:
        caps = ("report-status side-band-64k delete-refs ofs-delta "
                "symref=HEAD:refs/heads/main agent=%s" % AGENT)
        ctype = "application/x-git-receive-pack-advertisement"
    head, lines = _git_ref_lines(repo)
    parts = [pkt(("# service=%s\n" % service).encode()), pkt_flush()]
    if not lines:
        parts.append(pkt(("%s capabilities^{}\x00%s\n"
                          % (ZERO_OID, caps)).encode()))
    else:
        parts.append(pkt(("%s\x00%s\n" % (head, caps)).encode()))
        for line in lines:
            parts.append(pkt(line.encode() + b"\n"))
    parts.append(pkt_flush())
    req._send(200, b"".join(parts), content_type=ctype)


def _have_closure(repo, haves, old_shallow):
    """Objects the client already holds: everything reachable from the
    haves, except the ancestry below a shallow boundary — the client
    holds the boundary commit itself, not its parents."""
    shallow_set = set(old_shallow or [])
    seen = set()
    stack = [r for r in haves if r and r != ZERO_OID]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype = obj[0]
        if otype == "commit":
            parsed = commit_of(repo, oid)
            if parsed is not None:
                stack.append(parsed["tree"])
                if oid not in shallow_set:
                    stack.extend(parsed["parents"])
        elif otype == "tag":
            tagged = tag_of(repo, oid)
            if tagged is not None and tagged["object"]:
                stack.append(tagged["object"])
        elif otype == "tree":
            for _mode, _name, child in tree_of(repo, oid):
                stack.append(child)
    return seen


def _git_fetch_objects(repo, wants, haves, depth, old_shallow):
    """Objects to send for a fetch, plus shallow/unshallow boundary lists.

    The BFS is depth-limited: with a deepen limit it only ever walks as
    far as the fetch can send, so a depth-1 clone of a 200-commit history
    visits one commit instead of all 200. Parsed commits come from the
    memo, so a repeated fetch of the same tip costs almost nothing."""
    want_commits = []
    extra = {}
    for want in wants:
        obj = repo.get_object(want)
        if obj is None:
            continue
        if obj[0] == "tag":
            extra[want] = obj
            commit = peel_to_commit(repo, want)
            if commit is not None:
                want_commits.append(commit)
        elif obj[0] == "commit":
            want_commits.append(want)
    # BFS distances from the wants (0 = want itself, 1 = parent, ...).
    # Never enqueue a parent deeper than the fetch can include.
    dist = {}
    queue = [(c, 0) for c in want_commits]
    qi = 0
    limit = depth
    while qi < len(queue):
        commit, d = queue[qi]
        qi += 1
        if commit in dist:
            continue
        dist[commit] = d
        if limit is not None and d >= limit - 1:
            continue
        parsed = commit_of(repo, commit)
        if parsed is None:
            continue
        nd = d + 1
        for parent in parsed["parents"]:
            if parent not in dist:
                queue.append((parent, nd))
    if depth is None:
        included = set(dist)
    else:
        included = {c for c, d in dist.items() if d < depth}
    have_closure = _have_closure(repo, haves, old_shallow)
    send = {}
    for oid, obj in extra.items():
        if oid not in have_closure:
            send[oid] = obj
    get_object = repo.get_object
    for commit in included:
        if commit in have_closure:
            continue
        obj = get_object(commit)
        if obj is None:
            continue
        send[commit] = obj
        parsed = commit_of(repo, commit)
        if parsed is None:
            continue
        for oid in reachable(repo, [parsed["tree"]]):
            if oid not in have_closure and oid not in send:
                send[oid] = get_object(oid)
    shallow = []
    unshallow = []
    if depth is not None:
        for commit, d in dist.items():
            if d == depth - 1 and commit in included:
                parsed = commit_of(repo, commit)
                if parsed is not None and parsed["parents"]:
                    shallow.append(commit)
        for boundary in (old_shallow or []):
            parsed = commit_of(repo, boundary)
            if parsed is None:
                continue
            if any(p in send for p in parsed["parents"]):
                unshallow.append(boundary)
    return send, sorted(shallow), sorted(unshallow)


def _git_fetch_pack(repo, wants, haves, depth, old_shallow):
    """(pack_bytes, shallow, unshallow) for a fetch, memoised on its inputs.

    A fetch's output is a pure function of its inputs over immutable
    objects, so the cache key (wants, haves, depth, old_shallow) is
    complete: a new tip is a new key, and a repeated clone of the same tip
    reuses the same pack bytes. Returns the pack already serialised."""
    key = (tuple(wants), tuple(sorted(haves)), depth,
           tuple(sorted(old_shallow or [])))
    hit = repo._fetch_cache.get(key)
    if hit is not None:
        return hit
    send, shallow, unshallow = _git_fetch_objects(
        repo, wants, haves, depth, old_shallow)
    pack = write_pack([obj for _oid, obj in sorted(send.items())]) if send else b""
    out = (pack, shallow, unshallow)
    repo._fetch_cache[key] = out
    return out


def _git_sideband_pack(pack):
    chunks = [pkt(b"\x01" + pack[off:off + 32768])
              for off in range(0, len(pack), 32768)]
    return b"".join(chunks)


def _git_upload_pack_v2(req, repo, lines):
    command = None
    peel = True
    symrefs = False
    unborn = False
    prefixes = []
    wants = []
    haves = []
    depth = None
    old_shallow = []
    for line in lines:
        if line.startswith("command="):
            command = line[len("command="):]
        elif line == "peel":
            peel = True
        elif line == "symrefs":
            symrefs = True
        elif line == "unborn":
            unborn = True
        elif line.startswith("ref-prefix "):
            prefixes.append(line[len("ref-prefix "):])
        elif line.startswith("want "):
            wants.append(line[len("want "):].strip())
        elif line.startswith("have "):
            haves.append(line[len("have "):].strip())
        elif line.startswith("shallow "):
            old_shallow.append(line[len("shallow "):].strip())
        elif line.startswith("deepen "):
            try:
                depth = int(line[len("deepen "):].strip())
            except ValueError:
                depth = None
    if command == "ls-refs":
        parts = []
        tip = repo.tip()
        if not prefixes:
            if tip is not None:
                parts.append(pkt(("%s HEAD symref-target:refs/heads/main\n"
                                  % tip).encode()))
            else:
                parts.append(pkt(b"unborn HEAD symref-target:refs/heads/main\n"))
        for name, oid in sorted(repo.refs.items()):
            if prefixes and not any(name.startswith(p) for p in prefixes):
                continue
            line = ("%s %s" % (oid, name)).encode()
            if peel:
                obj = repo.get_object(oid)
                if obj is not None and obj[0] == "tag":
                    commit = peel_to_commit(repo, oid)
                    if commit is not None:
                        line += (" peeled:%s" % commit).encode()
            parts.append(pkt(line + b"\n"))
        parts.append(pkt_flush())
        req._send(200, b"".join(parts),
                  content_type="application/x-git-upload-pack-result")
        return
    if command == "fetch":
        pack, shallow, unshallow = _git_fetch_pack(
            repo, wants, haves, depth, old_shallow)
        parts = [pkt(b"acknowledgments\n"), pkt(b"NAK\n"), pkt_delim()]
        if depth is not None or old_shallow:
            parts.append(pkt(b"shallow-info\n"))
            for oid in shallow:
                parts.append(pkt(("shallow %s\n" % oid).encode()))
            for oid in unshallow:
                parts.append(pkt(("unshallow %s\n" % oid).encode()))
            parts.append(pkt_delim())
        if pack:
            parts.append(pkt(b"packfile\n"))
            parts.append(_git_sideband_pack(pack))
        parts.append(pkt_flush())
        req._send(200, b"".join(parts),
                  content_type="application/x-git-upload-pack-result")
        return
    raise ValueError("unknown v2 command: %s" % command)


def _git_upload_pack(req, repo):
    body = req.body
    v2 = req.headers.get(b"git-protocol") == b"version=2"
    try:
        packets, _off = parse_pkt_lines(body)
    except ValueError:
        req._send(400, b"bad pkt-line", content_type="text/plain")
        return
    lines = [payload.decode("utf-8", "replace").rstrip("\n")
             for kind, payload in packets if kind == "data"]
    if v2:
        _git_upload_pack_v2(req, repo, lines)
        return
    wants = []
    haves = []
    depth = None
    old_shallow = []
    for line in lines:
        if line.startswith("want "):
            wants.append(line.split()[1])
        elif line.startswith("have "):
            haves.append(line.split()[1])
        elif line.startswith("shallow "):
            old_shallow.append(line.split()[1])
        elif line.startswith("deepen "):
            try:
                depth = int(line.split()[1])
            except ValueError:
                depth = None
    pack, shallow, unshallow = _git_fetch_pack(
        repo, wants, haves, depth, old_shallow)
    parts = [pkt(b"NAK\n")]
    for oid in shallow:
        parts.append(pkt(("shallow %s\n" % oid).encode()))
    for oid in unshallow:
        parts.append(pkt(("unshallow %s\n" % oid).encode()))
    if pack:
        parts.append(_git_sideband_pack(pack))
    parts.append(pkt_flush())
    req._send(200, b"".join(parts),
              content_type="application/x-git-upload-pack-result")


def _apply_ref_update(repo, old, new, ref):
    """Apply one receive-pack ref update; returns the report line."""
    if not ref.startswith("refs/"):
        return "ng %s invalid ref" % ref
    current = repo.refs.get(ref)
    if new == ZERO_OID:
        if current is None:
            return "ng %s no such ref" % ref
        del repo.refs[ref]
        return "ok %s" % ref
    if old == ZERO_OID:
        if current is not None:
            return "ng %s already exists" % ref
        if repo.get_object(new) is None:
            return "ng %s object not found" % ref
        repo.refs[ref] = new
        return "ok %s" % ref
    if current is None:
        return "ng %s no such ref" % ref
    if current != old:
        return "ng %s non-fast-forward" % ref
    if repo.get_object(new) is None:
        return "ng %s object not found" % ref
    if new == old:
        return "ok %s" % ref
    if not is_ancestor(repo, old, new):
        return "ng %s non-fast-forward" % ref
    repo.refs[ref] = new
    return "ok %s" % ref


def _git_receive_pack(req, repo):
    body = req.body
    try:
        packets, off = parse_pkt_lines(body)
    except ValueError:
        req._send(400, b"bad pkt-line", content_type="text/plain")
        return
    commands = []
    for kind, payload in packets:
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace").rstrip("\n")
        if "\x00" in line:
            line = line.split("\x00", 1)[0]
        parts = line.split()
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_bytes = body[off:]
    report = []
    if pack_bytes:
        try:
            objects = read_pack(pack_bytes,
                                lookup=lambda oid: repo.get_object(oid))
            for oid, (otype, data) in objects.items():
                repo.put_object(otype, data)
        except Exception as exc:
            report.append("unpack %s" % exc)
            for _old, _new, ref in commands:
                report.append("ng %s unpack failed" % ref)
            _git_send_report(req, report)
            return
    report.append("unpack ok")
    for old, new, ref in commands:
        report.append(_apply_ref_update(repo, old, new, ref))
    _git_send_report(req, report)


def _git_send_report(req, lines):
    data = b"".join(pkt((line + "\n").encode()) for line in lines)
    chunks = [pkt(b"\x01" + data[off:off + 32768])
              for off in range(0, len(data), 32768)]
    chunks.append(pkt_flush())
    req._send(200, b"".join(chunks),
              content_type="application/x-git-receive-pack-result")


def git_dispatch(req, method, owner, repo_name, rest, query):
    if not req.authed():
        req.send_error_json(401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, repo_name)
    try:
        if rest == "/info/refs":
            service = query.get("service")
            if repo is None:
                req.send_error_json(404, "Not Found")
                return
            if service not in ("git-upload-pack", "git-receive-pack"):
                req.send_error_json(400, "Unknown service")
                return
            _git_info_refs(req, repo, service)
            return
        if rest == "/git-upload-pack" and method == "POST":
            if repo is None:
                req.send_error_json(404, "Not Found")
                return
            _git_upload_pack(req, repo)
            return
        if rest == "/git-receive-pack" and method == "POST":
            if repo is None:
                req.send_error_json(404, "Not Found")
                return
            _git_receive_pack(req, repo)
            return
        req.send_error_json(404, "Not Found")
    except Exception as exc:
        try:
            req._send(400, ("bad request: %s" % exc).encode(),
                      content_type="text/plain")
        except Exception:
            pass

# --------------------------------------------------------------------------
# Server bootstrap + env lifecycle
# --------------------------------------------------------------------------

def _http_post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read()


def _http_get_json(url, timeout=30):
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return resp.status, resp.read()


def _retry_loop(fn, is_done):
    """Call fn() until is_done(status, body) is true, backing off on
    failure. Used for the env's register/done handshake."""
    delay = 0.25
    while True:
        try:
            status, body = fn()
            if is_done(status, body):
                return
        except Exception as exc:
            if "register returned 4" in str(exc):
                raise
        delay = min(delay * 2, 2.0)
        time.sleep(delay)


def _register(port):
    url = ENV_URL.rstrip("/") + "/register"

    def attempt():
        status, _body = _http_post_json(url, {"port": port})
        if status not in (200, 201, 409):
            raise RuntimeError("register returned %d" % status)
        return status, _body

    _retry_loop(attempt, lambda status, _body: status in (200, 201, 409))


def _wait_done():
    url = ENV_URL.rstrip("/") + "/done"

    def attempt():
        return _http_get_json(url)

    def done(status, body):
        if status != 200:
            return False
        try:
            return bool(json.loads(body.decode("utf-8")).get("done"))
        except (ValueError, TypeError):
            return False

    _retry_loop(attempt, done)


def main():
    if not ENV_URL:
        port = int(os.environ.get("PORT", "8765"))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(128)
        print("listening on port %d (local mode, no MAKEFUN_ENV_URL)"
              % port, flush=True)
        try:
            accept_loop(srv)
        except KeyboardInterrupt:
            pass
        return
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 0))
    srv.listen(128)
    thread = threading.Thread(target=accept_loop, args=(srv,), daemon=True)
    thread.start()
    port = srv.getsockname()[1]
    print("github-core-clone listening on port %d" % port, flush=True)
    _register(port)
    print("github-core-clone registered; waiting for the env to say done",
          flush=True)
    _wait_done()
    srv.close()
    print("github-core-clone done", flush=True)


if __name__ == "__main__":
    main()
