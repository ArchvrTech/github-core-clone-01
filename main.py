#!/usr/bin/env python3
"""github-core-clone-01 entry: a standalone GitHub-compatible git forge.

One process, Python 3.13 stdlib only. Serves git smart HTTP (upload-pack v2 +
v0 fallback, receive-pack v0), REST v3 under /api/v3, GraphQL v4 at
/api/graphql and MCP at /mcp, all from an in-memory store. No git binary, no
network beyond the trial env.

Lifecycle (per the kit's wire contract): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, then long-poll GET
$MAKEFUN_ENV_URL/done until {"done": true} and exit 0.

The viewer login below is the scenario's fixed viewer: the kit's wire.ts
builds its scenario from the public constant SCENARIO_SEED
(0x6a09e667f3bcc909), identical for every trial, and the viewer it yields is
adaiversen (id 1000). Verified by running the kit's own buildScenario()
during the build. The bearer token in MAKEFUN_SHOP_TOKEN names that login.
"""

import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
import urllib.request
import urllib.error
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

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

_req_counter = [0]
_req_lock = threading.Lock()


def next_request_id():
    with _req_lock:
        _req_counter[0] += 1
        return "req-%d" % _req_counter[0]


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def gid(kind: str, num: int) -> str:
    """Opaque Relay-style global id, stable across reads."""
    return "%s_%s" % (kind, b64url(("%s:%d" % (kind, num)).encode()))


def parse_gid(value):
    """Decode a global id made by gid(); returns (kind, num) or None."""
    if not isinstance(value, str):
        return None
    m = re.match(r"^([A-Za-z]+)_([A-Za-z0-9_-]+)$", value)
    if not m:
        return None
    try:
        raw = base64.urlsafe_b64decode(m.group(2) + "=" * (-len(m.group(2)) % 4))
        kind, num = raw.decode().split(":")
    except Exception:
        return None
    if kind != m.group(1):
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
    header = ("%s %d\0" % (otype, len(data))).encode()
    return hashlib.sha1(header + data).hexdigest()


def parse_tree(data: bytes):
    """Return [(mode, name, oid_hex)] for a tree object's raw bytes."""
    entries = []
    i = 0
    n = len(data)
    while i < n:
        sp = data.index(b" ", i)
        mode = data[i:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1:nul].decode("utf-8", "surrogateescape")
        oid = data[nul + 1:nul + 21].hex()
        entries.append((mode, name, oid))
        i = nul + 21
    return entries


def _tree_sort_key(entry):
    mode, name, _oid = entry
    # git sorts tree entries as if directory names had a trailing slash
    return name + ("/" if mode == "40000" else "")


def build_tree(entries):
    out = bytearray()
    for mode, name, oid in sorted(entries, key=_tree_sort_key):
        out += mode.encode() + b" " + name.encode("utf-8", "surrogateescape")
        out += b"\0" + bytes.fromhex(oid)
    return bytes(out)


def parse_commit(data: bytes):
    """Return {'tree': oid, 'parents': [oid], 'message': str, 'author': str,
    'committer': str} for a commit object's raw bytes."""
    head, _, message = data.partition(b"\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in head.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return {"tree": tree, "parents": parents,
            "message": message.decode("utf-8", "surrogateescape"),
            "author": author, "committer": committer}


def build_commit(tree, parents, message, author=IDENTITY, committer=IDENTITY,
                 timestamp=FIXED_EPOCH):
    lines = ["tree %s" % tree]
    for p in parents:
        lines.append("parent %s" % p)
    lines.append("author %s %d +0000" % (author, timestamp))
    lines.append("committer %s %d +0000" % (committer, timestamp))
    if not message.endswith("\n"):
        message += "\n"
    return ("\n".join(lines) + "\n\n" + message).encode()


def parse_tag(data: bytes):
    head, _, message = data.partition(b"\n\n")
    out = {"object": None, "type": None, "tag": None, "tagger": "",
           "message": message.decode("utf-8", "surrogateescape")}
    for line in head.decode("utf-8", "surrogateescape").split("\n"):
        if line.startswith("object "):
            out["object"] = line[7:].strip()
        elif line.startswith("type "):
            out["type"] = line[5:].strip()
        elif line.startswith("tag "):
            out["tag"] = line[4:].strip()
        elif line.startswith("tagger "):
            out["tagger"] = line[7:]
    return out


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
    where packets are ('data', bytes) | ('flush',) | ('delim',). Stops at the
    first flush packet; rest_offset is the byte just past it."""
    packets = []
    i = 0
    n = len(buf)
    while i + 4 <= n:
        size_hex = buf[i:i + 4]
        try:
            size = int(size_hex.decode("ascii"), 16)
        except ValueError:
            raise ValueError("bad pkt-line length %r" % size_hex)
        if size == 0:
            return packets, i + 4
        if size == 1:
            packets.append(("delim", b""))
            i += 4
            continue
        if size < 4 or i + size > n:
            raise ValueError("truncated pkt-line")
        packets.append(("data", bytes(buf[i + 4:i + size])))
        i += size
    return packets, i


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
    d = zlib.decompressobj()
    out = d.decompress(bytes(buf[off:]))
    consumed = len(buf) - off - len(d.unused_data)
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

    src_size, off = read_varint(0)
    tgt_size, off = read_varint(off)
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    out = bytearray()
    while off < len(delta):
        op = delta[off]
        off += 1
        if op & 0x80:
            cp_off = 0
            cp_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    cp_off |= delta[off] << (bit * 8)
                    off += 1
            for bit in range(4, 7):
                if op & (1 << bit):
                    cp_size |= delta[off] << ((bit - 4) * 8)
                    off += 1
            if cp_size == 0:
                cp_size = 0x10000
            out += base[cp_off:cp_off + cp_size]
        elif op:
            out += delta[off:off + op]
            off += op
        else:
            raise ValueError("delta opcode 0")
    if len(out) != tgt_size:
        raise ValueError("delta size mismatch")
    return bytes(out)


def read_pack(pack: bytes, lookup=None):
    """Read a packfile. Returns {oid: (type, data)}. lookup(oid) may supply a
    base object a thin pack leaves out. Raises ValueError on corruption."""
    if len(pack) < 32 or pack[:4] != b"PACK":
        raise ValueError("not a packfile")
    version = int.from_bytes(pack[4:8], "big")
    if version not in (2, 3):
        raise ValueError("unsupported pack version %d" % version)
    count = int.from_bytes(pack[8:12], "big")
    trailer = pack[-20:].hex()
    if sha1(pack[:-20]) != trailer:
        raise ValueError("pack checksum mismatch")
    entries = []  # (offset, type_code, size, data_or_delta, base_ref)
    off = 12
    for _ in range(count):
        start = off
        first = pack[off]
        type_code = (first >> 4) & 0x07
        size, off = _read_varint(pack, off)
        if type_code in (1, 2, 3, 4):
            data, off = _inflate_at(pack, off)
            entries.append((start, type_code, size, data, None))
        elif type_code == 6:  # OFS_DELTA
            byte = pack[off]
            off += 1
            base_off = byte & 0x7F
            while byte & 0x80:
                byte = pack[off]
                off += 1
                base_off = ((base_off + 1) << 7) | (byte & 0x7F)
            delta, off = _inflate_at(pack, off)
            entries.append((start, 6, size, delta, ("ofs", start - base_off)))
        elif type_code == 7:  # REF_DELTA
            base_oid = pack[off:off + 20].hex()
            off += 20
            delta, off = _inflate_at(pack, off)
            entries.append((start, 7, size, delta, ("ref", base_oid)))
        else:
            raise ValueError("unknown pack entry type %d" % type_code)
    by_offset = {}
    objects = {}

    def resolve_entry(entry):
        start, type_code, size, data, base_ref = entry
        if type_code in (1, 2, 3, 4):
            otype = _TYPE_CODES[type_code]
            if len(data) != size:
                raise ValueError("pack object size mismatch")
            oid = hash_object(otype, data)
            by_offset[start] = (otype, data, oid)
            objects[oid] = (otype, data)
            return oid
        # delta entry
        if base_ref[0] == "ofs":
            base = by_offset.get(base_ref[1])
            if base is None:
                # base entry comes later in the pack; resolve it first
                for other in entries:
                    if other[0] == base_ref[1]:
                        resolve_entry(other)
                        base = by_offset.get(base_ref[1])
                        break
            if base is None:
                raise ValueError("ofs-delta base missing")
            base_type, base_data, _ = base
        else:
            base = objects.get(base_ref[1])
            if base is None and lookup is not None:
                base = lookup(base_ref[1])
            if base is None:
                raise ValueError("ref-delta base missing")
            base_type, base_data = base[0], base[1]
        resolved = apply_delta(base_data, data)
        if len(resolved) != size:
            raise ValueError("delta resolved size mismatch")
        oid = hash_object(base_type, resolved)
        by_offset[start] = (base_type, resolved, oid)
        objects[oid] = (base_type, resolved)
        return oid

    for entry in entries:
        resolve_entry(entry)
    return objects


def write_pack(objects):
    """objects: iterable of (type, data). Full objects only, no deltas."""
    items = list(objects)
    out = bytearray()
    out += b"PACK" + (2).to_bytes(4, "big") + len(items).to_bytes(4, "big")
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
        out += bytes(header) + zlib.compress(data, 6)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)

# --------------------------------------------------------------------------
# in-memory store
# --------------------------------------------------------------------------

class Repo:
    __slots__ = ("name", "owner", "db_id", "private", "description",
                 "default_branch", "created_seq", "objects", "refs",
                 "issues", "labels", "pulls", "next_number", "next_comment")

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
    out = {}
    stack = [(tree_oid, "")]
    while stack:
        oid, prefix = stack.pop()
        obj = repo.get_object(oid)
        if obj is None or obj[0] != "tree":
            continue
        for mode, name, child in parse_tree(obj[1]):
            path = prefix + name
            if mode == "40000":
                stack.append((child, path + "/"))
            else:
                out[path] = (mode, child)
    return out


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
    obj = repo.get_object(commit_oid)
    if obj is None or obj[0] != "commit":
        return None
    return parse_commit(obj[1])["tree"]


def reachable(repo, roots, want_types=None):
    """All oids reachable from roots (commits, tags, trees, blobs)."""
    seen = set()
    stack = [r for r in roots if r and r != ZERO_OID]
    while stack:
        oid = stack.pop()
        if oid in seen:
            continue
        obj = repo.get_object(oid)
        if obj is None:
            continue
        seen.add(oid)
        otype, data = obj
        if otype == "commit":
            c = parse_commit(data)
            stack.append(c["tree"])
            stack.extend(c["parents"])
        elif otype == "tag":
            stack.append(parse_tag(data)["object"])
        elif otype == "tree":
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
    if want_types is not None:
        return {o for o in seen if repo.get_object(o)[0] in want_types}
    return seen


def commit_ancestors(repo, commit_oid):
    """Set of commit oids reachable from commit_oid following parents."""
    seen = set()
    stack = [commit_oid]
    while stack:
        oid = stack.pop()
        if oid in seen or oid == ZERO_OID:
            continue
        obj = repo.get_object(oid)
        if obj is None or obj[0] != "commit":
            continue
        seen.add(oid)
        stack.extend(parse_commit(obj[1])["parents"])
    return seen


def is_ancestor(repo, anc, desc):
    if anc == desc:
        return True
    return anc in commit_ancestors(repo, desc)


def peel_to_commit(repo, oid):
    """Follow annotated tags to the commit; returns commit oid or None."""
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
            cur = parse_tag(obj[1])["object"]
            continue
        return None
    return None


def diff_trees(repo, base_tree_oid, head_tree_oid):
    """Return list of (path, status, old_oid, new_oid) comparing two trees."""
    base = tree_flat(repo, base_tree_oid) if base_tree_oid else {}
    head = tree_flat(repo, head_tree_oid) if head_tree_oid else {}
    out = []
    for path in sorted(set(base) | set(head)):
        b = base.get(path)
        h = head.get(path)
        if b is None:
            out.append((path, "added", None, h[1]))
        elif h is None:
            out.append((path, "removed", b[1], None))
        elif b[1] != h[1]:
            out.append((path, "modified", b[1], h[1]))
    return out


def line_counts(repo, old_oid, new_oid):
    """(additions, deletions) between two blob oids, by line multiset diff."""
    def lines(oid):
        if oid is None:
            return []
        obj = repo.get_object(oid)
        if obj is None:
            return []
        return obj[1].decode("utf-8", "surrogateescape").splitlines()
    old = lines(old_oid)
    new = lines(new_oid)
    from collections import Counter
    co, cn = Counter(old), Counter(new)
    additions = sum((cn - co).values())
    deletions = sum((co - cn).values())
    return additions, deletions


def make_commit_on_branch(repo, branch, files, message, base_tree=None):
    """Create a commit on branch replacing/adding files {path: bytes|None}
    (None deletes). Returns commit oid. Branch may be unborn."""
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
# HTTP plumbing
# --------------------------------------------------------------------------

def json_bytes(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "github-core-clone/1.0"

    def setup(self):
        super().setup()
        # Disable Nagle: the suite makes thousands of tiny sequential
        # requests on one keep-alive connection, and the delayed-ACK
        # interaction otherwise costs ~20 ms per round trip.
        try:
            self.request.setsockopt(socket.IPPROTO_TCP,
                                    socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass

    def log_message(self, *args):
        pass

    # -- low-level send helpers -------------------------------------------
    def _send(self, status, body: bytes, content_type="application/json",
              extra=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("X-GitHub-Request-Id", next_request_id())
            if extra:
                for k, v in extra.items():
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status, obj, extra=None):
        self._send(status, json_bytes(obj), extra=extra)

    def send_error_json(self, status, message):
        self.send_json(status, {"message": message})

    def send_204(self):
        try:
            self.send_response(204)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-GitHub-Request-Id", next_request_id())
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_body(self):
        if getattr(self, "_body", None) is not None:
            return self._body
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            n = int(length)
        except ValueError:
            return b""
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def read_json_body(self):
        """Returns (obj, error). obj is {} when the body is empty."""
        raw = self.read_body()
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
        host = self.headers.get("Host", "127.0.0.1")
        return "http://" + host

    def authed(self):
        if not TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        return auth == "Bearer " + TOKEN

    # -- dispatch ----------------------------------------------------------
    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PUT(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def _dispatch(self):
        # Drain the request body before any response is sent, so a
        # keep-alive connection never desyncs when we answer 401/400
        # without otherwise touching the body. Reset first: the handler
        # instance is reused across keep-alive requests, so a stale
        # buffered body must never leak into the next request.
        self._body = None
        self._body = self.read_body()
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # never crash the server on a bad request
            try:
                self.send_error_json(500, "Internal error: %s" % exc)
            except Exception:
                pass

    def _route(self):
        parsed = urlparse(self.path)
        path = parsed.path
        method = self.command
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if method == "GET" and path == "/healthz":
            self.send_json(200, {"ok": True})
            return

        if path == "/api/graphql" or path.startswith("/api/graphql/"):
            if method == "POST":
                self._graphql()
            else:
                self.send_error_json(404, "Not Found")
            return

        if path == "/mcp":
            if method == "POST":
                self._mcp()
            elif method == "DELETE":
                self.send_204()
            else:
                self.send_error_json(404, "Not Found")
            return

        if path.startswith("/api/v3"):
            self._rest(method, path[len("/api/v3"):], query)
            return

        m = re.match(r"^/([^/]+)/([^/]+)\.git(/.*)?$", path)
        if m:
            owner, repo_name = unquote(m.group(1)), unquote(m.group(2))
            rest = m.group(3) or ""
            self._git(method, owner, repo_name, rest, query)
            return

        self.send_error_json(404, "Not Found")

    # -- REST ---------------------------------------------------------------
    def _rest(self, method, sub, query):
        if not self.authed():
            self.send_error_json(401, "Requires authentication")
            return
        segments = [unquote(s) for s in sub.strip("/").split("/") if s != ""]
        try:
            self._rest_routed(method, segments, query)
        except _HttpError as err:
            self.send_error_json(err.status, err.message)

    def _rest_routed(self, method, seg, query):
        # API root
        if not seg:
            if method == "GET":
                self.send_json(200, {
                    "current_user_url": "/api/v3/user",
                    "rate_limit_url": "/api/v3/rate_limit",
                    "repository_url": "/api/v3/repos/{owner}/{repo}",
                }, extra={"X-OAuth-Scopes": "repo, read:org"})
                return
            raise _HttpError(404, "Not Found")
        if seg == ["meta"]:
            if method == "GET":
                self.send_json(200, {"installed_version": VERSION})
                return
            raise _HttpError(404, "Not Found")
        if seg == ["user"]:
            if method == "GET":
                self.send_json(200, _user_json())
                return
            raise _HttpError(404, "Not Found")
        if seg == ["user", "repos"]:
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
                    self.send_json(200, _repo_json(repo))
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

    def _repo_subroute(self, repo, method, rest, query):
        if rest == ["branches"]:
            if method == "GET":
                branches = []
                for refname in sorted(repo.refs):
                    if refname.startswith("refs/heads/"):
                        branches.append({"name": refname[len("refs/heads/"):],
                                         "commit": {"sha": repo.refs[refname]},
                                         "protected": False})
                self.send_json(200, branches)
                return
            raise _HttpError(404, "Not Found")
        if len(rest) >= 2 and rest[0] == "branches":
            if method == "GET":
                refname = "refs/heads/" + "/".join(rest[1:])
                # branch names may hold slashes; rest[1:] already joined above
                tip = repo.refs.get(refname)
                if tip is None:
                    raise _HttpError(404, "Not Found")
                self.send_json(200, {"name": "/".join(rest[1:]),
                                     "commit": {"sha": tip}})
                return
            raise _HttpError(404, "Not Found")
        if rest == ["commits"]:
            if method == "GET":
                self._list_commits(repo, query.get("sha"))
                return
            raise _HttpError(404, "Not Found")
        if rest == ["git", "refs"]:
            if method == "POST":
                body, err = self.read_json_body()
                if err:
                    raise _HttpError(400, err)
                self._create_ref(repo, body)
                return
            raise _HttpError(404, "Not Found")
        if len(rest) >= 4 and rest[0] == "git" and rest[1] == "refs" \
                and rest[2] == "heads":
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
        if rest[0] == "contents":
            self._contents(repo, method, rest[1:], query)
            return
        if rest == ["labels"]:
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
                self.send_json(201, label)
                return
            raise _HttpError(404, "Not Found")
        if rest == ["issues"]:
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
        if len(rest) >= 2 and rest[0] == "issues":
            self._issue_subroute(repo, method, rest[1:], query)
            return
        if rest == ["pulls"]:
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
        if len(rest) >= 2 and rest[0] == "pulls":
            self._pull_subroute(repo, method, rest[1:], query)
            return
        raise _HttpError(404, "Not Found")

    # -- repo helpers --------------------------------------------------------
    def _list_user_repos(self, query):
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
        self.send_json(200, [_repo_json(r) for r in items], extra=extra)

    def _create_repo(self, body):
        name = body.get("name")
        if not isinstance(name, str) or not name:
            raise _HttpError(422, "name is required")
        repo = STORE.create_repo(name, body.get("private", False),
                                 body.get("description", ""))
        if repo is None:
            raise _HttpError(422, "Repository already exists")
        if isinstance(body.get("default_branch"), str) and body["default_branch"]:
            repo.default_branch = body["default_branch"]
        self.send_json(201, _repo_json(repo))

    def _patch_repo(self, repo, body):
        if isinstance(body.get("description"), str):
            repo.description = body["description"]
        if isinstance(body.get("private"), bool):
            repo.private = body["private"]
        self.send_json(200, _repo_json(repo))

    def _create_ref(self, repo, body):
        ref = body.get("ref")
        sha = body.get("sha")
        if not isinstance(ref, str) or not ref.startswith("refs/"):
            raise _HttpError(422, "Invalid ref")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise _HttpError(422, "Invalid sha")
        if repo.get_object(sha) is None or repo.get_object(sha)[0] != "commit":
            raise _HttpError(422, "Invalid sha")
        if ref in repo.refs:
            raise _HttpError(422, "Reference already exists")
        repo.refs[ref] = sha
        self.send_json(201, {"ref": ref,
                             "object": {"sha": sha, "type": "commit"}})

    def _list_commits(self, repo, ref):
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
            obj = repo.get_object(oid)
            if obj is None or obj[0] != "commit":
                continue
            c = parse_commit(obj[1])
            message = c["message"]
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
                        "parents": [{"sha": p} for p in c["parents"]]})
            stack.extend(c["parents"])
        self.send_json(200, out)

    # -- contents ------------------------------------------------------------
    def _resolve_ref_tip(self, repo, ref):
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

    def _tree_entry_at(self, repo, tree_oid, path):
        """(mode, oid) of path inside tree_oid, or None."""
        if not path:
            return ("40000", tree_oid)
        cur_oid = tree_oid
        for part in path.split("/"):
            obj = repo.get_object(cur_oid)
            if obj is None or obj[0] != "tree":
                return None
            found = None
            for mode, name, child in parse_tree(obj[1]):
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

    def _contents(self, repo, method, path_parts, query):
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
            obj = repo.get_object(oid)
            items = []
            for mode, name, child in parse_tree(obj[1]):
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

    def _contents_put(self, repo, path, body):
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

    def _contents_delete(self, repo, path, body):
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


class _HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _page_int(value, default):
    try:
        n = int(value)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def _user_json():
    return {"login": VIEWER_LOGIN, "id": VIEWER_ID, "node_id": gid("U", VIEWER_ID),
            "type": "User", "name": VIEWER_NAME,
            "email": VIEWER_EMAIL}


def _repo_json(repo):
    return {"id": repo.db_id, "node_id": repo.global_id(),
            "name": repo.name,
            "full_name": "%s/%s" % (repo.owner, repo.name),
            "owner": {"login": repo.owner, "id": VIEWER_ID, "type": "User"},
            "private": repo.private, "description": repo.description,
            "default_branch": repo.default_branch,
            "html_url": "https://example.invalid/%s/%s" % (repo.owner, repo.name),
            "url": "/api/v3/repos/%s/%s" % (repo.owner, repo.name),
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "has_issues": True, "has_projects": True, "has_wiki": True,
            "fork": False, "archived": False, "disabled": False}

# -- issues --------------------------------------------------------------
def _issue_json(self, repo, issue):
    return {"id": issue["id"], "node_id": gid("I", issue["id"]),
            "number": issue["number"], "title": issue["title"],
            "body": issue["body"], "state": issue["state"],
            "labels": [{"name": n, "color": repo.labels.get(n, {}).get("color", "ededed")}
                       for n in issue["labels"]],
            "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "comments": len(issue["comments"]),
            "html_url": "https://example.invalid/%s/%s/issues/%d"
                        % (repo.owner, repo.name, issue["number"])}

def _list_issues(self, repo, query):
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
    self.send_json(200, [self._issue_json(repo, i) for i in page_items],
                   extra=extra)

def _create_issue(self, repo, body):
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
             "comments": [], "seq": issue_id}
    repo.issues[number] = issue
    self.send_json(201, self._issue_json(repo, issue))

def _issue_subroute(self, repo, method, rest, query):
    try:
        number = int(rest[0])
    except ValueError:
        raise _HttpError(404, "Not Found")
    issue = repo.issues.get(number)
    if len(rest) == 1:
        if issue is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            self.send_json(200, self._issue_json(repo, issue))
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
            self.send_json(200, self._issue_json(repo, issue))
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
            self.send_json(201, {
                "id": cid, "node_id": comment["node_id"],
                "body": comment["body"],
                "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
                "created_at": FIXED_TS})
            return
        raise _HttpError(404, "Not Found")
    raise _HttpError(404, "Not Found")

# -- pull requests ---------------------------------------------------------
def _pr_json(self, repo, pr):
    head_tip = repo.refs.get("refs/heads/" + pr["head"]) or pr["head_sha"]
    base_tip = repo.refs.get("refs/heads/" + pr["base"]) or pr["base_sha"]
    return {"id": pr["id"], "node_id": gid("P", pr["id"]),
            "number": pr["number"], "title": pr["title"],
            "body": pr["body"], "state": pr["state"],
            "merged": pr["merged"],
            "mergeable": self._pr_mergeable(repo, pr),
            "merged_at": FIXED_TS if pr["merged"] else None,
            "merge_commit_sha": pr["merge_sha"],
            "head": {"ref": pr["head"], "sha": head_tip,
                     "label": "%s:%s" % (repo.owner, pr["head"]),
                     "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}},
            "base": {"ref": pr["base"], "sha": base_tip,
                     "label": "%s:%s" % (repo.owner, pr["base"]),
                     "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID}},
            "user": {"login": VIEWER_LOGIN, "id": VIEWER_ID},
            "created_at": FIXED_TS, "updated_at": FIXED_TS,
            "html_url": "https://example.invalid/%s/%s/pull/%d"
                        % (repo.owner, repo.name, pr["number"])}

def _pr_mergeable(self, repo, pr):
    if pr["state"] != "open":
        return False
    return self._merge_conflict(repo, pr) is None

def _merge_conflict(self, repo, pr):
    """Return a conflicting path or None."""
    base_tip = repo.refs.get("refs/heads/" + pr["base"])
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    if base_tip is None or head_tip is None:
        return None
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip)
    now_tree = commit_tree_oid(repo, base_tip)
    if base_tree is None or head_tree is None or now_tree is None:
        return None
    head_changes = {p: n for p, _s, _o, n in
                    diff_trees(repo, base_tree, head_tree)}
    base_changes = {p: n for p, _s, _o, n in
                    diff_trees(repo, base_tree, now_tree)}
    for path, new_oid in head_changes.items():
        if path in base_changes and base_changes[path] != new_oid:
            return path
    return None

def _list_pulls(self, repo, query):
    state = query.get("state", "open")
    items = list(repo.pulls.values())
    if state != "all":
        items = [p for p in items if p["state"] == state]
    items.sort(key=lambda p: p["number"])
    per_page = _page_int(query.get("per_page"), 30)
    page = _page_int(query.get("page"), 1)
    start = (page - 1) * per_page
    self.send_json(200, [self._pr_json(repo, p)
                         for p in items[start:start + per_page]])

def _create_pull(self, repo, body):
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
    self.send_json(201, self._pr_json(repo, pr))

def _pull_subroute(self, repo, method, rest, query):
    try:
        number = int(rest[0])
    except ValueError:
        raise _HttpError(404, "Not Found")
    pr = repo.pulls.get(number)
    if len(rest) == 1:
        if pr is None:
            raise _HttpError(404, "Not Found")
        if method == "GET":
            self.send_json(200, self._pr_json(repo, pr))
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

def _pull_files(self, repo, pr):
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip) if head_tip else None
    out = []
    for path, status, old_oid, new_oid in diff_trees(repo, base_tree,
                                                     head_tree):
        additions, deletions = line_counts(repo, old_oid, new_oid)
        out.append({"filename": path, "status": status,
                    "additions": additions, "deletions": deletions,
                    "changes": additions + deletions,
                    "sha": new_oid or old_oid,
                    "blob_url": "/api/v3/repos/%s/%s/git/blobs/%s"
                                % (repo.owner, repo.name,
                                   new_oid or old_oid)})
    self.send_json(200, out)

def _merge_pull(self, repo, pr, body):
    if pr["state"] != "open":
        raise _HttpError(405, "Pull request is not open")
    if self._merge_conflict(repo, pr) is not None:
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

# bind the issue/pull-request handlers onto Handler
Handler._issue_json = _issue_json
Handler._list_issues = _list_issues
Handler._create_issue = _create_issue
Handler._issue_subroute = _issue_subroute
Handler._pr_json = _pr_json
Handler._pr_mergeable = _pr_mergeable
Handler._merge_conflict = _merge_conflict
Handler._list_pulls = _list_pulls
Handler._create_pull = _create_pull
Handler._pull_subroute = _pull_subroute
Handler._pull_files = _pull_files
Handler._merge_pull = _merge_pull

# --------------------------------------------------------------------------
# GraphQL v4
# --------------------------------------------------------------------------

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
        if c in "{}()[]:!=$":
            toks.append((c, c))
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r",
                                '"': '"', "\\": "\\", "/": "/"}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            toks.append(("STRING", "".join(buf)))
            i = j + 1
            continue
        if c.isdigit() or (c == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            toks.append(("NUMBER", text[i:j]))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            toks.append(("NAME", text[i:j]))
            i = j
            continue
        raise ValueError("unexpected character %r" % c)
    return toks


class _GQLParser:
    def __init__(self, text):
        self.toks = _gql_tokens(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def expect(self, kind):
        tok = self.next()
        if tok[0] != kind:
            raise ValueError("expected %s, got %r" % (kind, tok))
        return tok

    def parse_document(self):
        ops = []
        while self.i < len(self.toks):
            kind = "query"
            name = None
            if self.peek()[0] == "NAME" and self.peek()[1] in ("query", "mutation"):
                kind = self.next()[1]
                if self.peek()[0] == "NAME":
                    name = self.next()[1]
            if self.peek()[0] == "(":
                self._skip_parens()
            ops.append({"kind": kind, "name": name,
                        "selection": self.parse_selection_set()})
        return ops

    def _skip_parens(self):
        depth = 0
        while self.i < len(self.toks):
            tok = self.next()
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
            self.next()
            if self.peek()[0] == "NAME" and self.peek()[1] == "on":
                self.next()
                type_name = self.next()[1]
                return {"fragment": True, "type": type_name,
                        "selection": self.parse_selection_set()}
            self.next()  # named fragment spread: unsupported, skip
            return {"fragment": True, "type": None, "selection": []}
        name = self.next()[1]
        alias = None
        if self.peek()[0] == ":":
            self.next()
            alias = name
            name = self.next()[1]
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
            key = self.next()[1]
            self.expect(":")
            args[key] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        tok = self.next()
        if tok[0] == "$":
            return ("var", self.next()[1])
        if tok[0] == "STRING":
            return ("lit", tok[1])
        if tok[0] == "NUMBER":
            try:
                return ("lit", int(tok[1]))
            except ValueError:
                return ("lit", float(tok[1]))
        if tok[0] == "NAME":
            if tok[1] == "true":
                return ("lit", True)
            if tok[1] == "false":
                return ("lit", False)
            if tok[1] == "null":
                return ("lit", None)
            return ("enum", tok[1])
        if tok[0] == "[":
            items = []
            while self.peek()[0] != "]":
                items.append(self.parse_value())
            self.expect("]")
            return ("list", items)
        if tok[0] == "{":
            obj = {}
            while self.peek()[0] != "}":
                key = self.next()[1]
                self.expect(":")
                obj[key] = self.parse_value()
            self.expect("}")
            return ("object", obj)
        raise ValueError("unexpected value token %r" % (tok,))


def _gql_resolve(node, variables):
    kind = node[0]
    if kind == "var":
        return variables.get(node[1])
    if kind == "lit":
        return node[1]
    if kind == "enum":
        return node[1]
    if kind == "list":
        return [_gql_resolve(x, variables) for x in node[1]]
    if kind == "object":
        return {k: _gql_resolve(v, variables) for k, v in node[1].items()}
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
    return base64.urlsafe_b64encode(
        ("cursor:%d" % number).encode()).decode()


def _gql_cursor_number(cursor):
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        text = raw.decode()
        if text.startswith("cursor:"):
            return int(text[len("cursor:"):])
    except Exception:
        pass
    return None

# --------------------------------------------------------------------------
# GraphQL execution
# --------------------------------------------------------------------------

def _gql_find_repo(rid):
    for r in STORE.list_repos():
        if r.db_id == rid:
            return r
    return None


def _gql_find_issue(iid):
    for r in STORE.list_repos():
        for iss in r.issues.values():
            if iss["id"] == iid:
                return r, iss
    return None, None


def _gql_find_pr(pid):
    for r in STORE.list_repos():
        for pr in r.pulls.values():
            if pr["id"] == pid:
                return r, pr
    return None, None


def _gql_create_issue(repo, title, body):
    with STORE.lock:
        number = repo.next_number
        repo.next_number += 1
        issue_id = STORE.next_issue_id
        STORE.next_issue_id += 1
    issue = {"id": issue_id, "number": number, "title": title or "",
             "body": body or "", "state": "open", "labels": [],
             "comments": [], "seq": issue_id}
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
    pr = {"id": pr_id, "number": number, "title": title or "", "body": "",
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
    head_changes = {p: n for p, _s, _o, n in diff_trees(repo, base_tree, head_tree)}
    base_changes = {p: n for p, _s, _o, n in diff_trees(repo, base_tree, now_tree)}
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
    desc = (isinstance(order_by, dict)
            and str(order_by.get("direction", "")).upper() == "DESC")
    items.sort(key=lambda i: i["seq"], reverse=desc)
    if after:
        num = _gql_cursor_number(after)
        if num is not None:
            idx = next((k for k, i in enumerate(items)
                        if i["number"] == num), None)
            if idx is not None:
                items = items[idx + 1:]
    page = items[:first] if isinstance(first, int) else items
    has_next = len(items) > len(page)
    end_cursor = _gql_cursor(page[-1]["number"]) if page else None
    return {"__type": "IssueConnection",
            "nodes": [{"__type": "Issue", "repo": repo, "issue": i}
                      for i in page],
            "pageInfo": {"__type": "PageInfo", "hasNextPage": has_next,
                         "endCursor": end_cursor}}


def _gql_pr_files(repo, pr, first):
    head_tip = repo.refs.get("refs/heads/" + pr["head"])
    base_tree = commit_tree_oid(repo, pr["base_sha"])
    head_tree = commit_tree_oid(repo, head_tip) if head_tip else None
    nodes = [{"__type": "PullRequestChangedFile", "path": p}
             for p, _s, _o, _n in diff_trees(repo, base_tree, head_tree)]
    if isinstance(first, int):
        nodes = nodes[:first]
    return {"__type": "PullRequestChangedFileConnection", "nodes": nodes}


def _gql_resolve_field(h, type_name, value, name, args):
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
            parsed = parse_gid(inp.get("repositoryId"))
            if parsed is None or parsed[0] != "R":
                raise ValueError("unknown repository")
            repo = _gql_find_repo(parsed[1])
            if repo is None:
                raise ValueError("unknown repository")
            iss = _gql_create_issue(repo, inp.get("title"), inp.get("body"))
            return True, {"__type": "CreateIssuePayload",
                          "issue": {"__type": "Issue", "repo": repo,
                                    "issue": iss}}
        if name == "closeIssue":
            inp = args.get("input") or {}
            parsed = parse_gid(inp.get("issueId"))
            if parsed is None or parsed[0] != "I":
                raise ValueError("unknown issue")
            repo, iss = _gql_find_issue(parsed[1])
            if iss is None:
                raise ValueError("unknown issue")
            iss["state"] = "closed"
            return True, {"__type": "CloseIssuePayload",
                          "issue": {"__type": "Issue", "repo": repo,
                                    "issue": iss}}
        if name == "createPullRequest":
            inp = args.get("input") or {}
            parsed = parse_gid(inp.get("repositoryId"))
            if parsed is None or parsed[0] != "R":
                raise ValueError("unknown repository")
            repo = _gql_find_repo(parsed[1])
            if repo is None:
                raise ValueError("unknown repository")
            pr = _gql_create_pull(repo, inp.get("title"),
                                  inp.get("headRefName"), inp.get("baseRefName"))
            return True, {"__type": "CreatePullRequestPayload",
                          "pullRequest": {"__type": "PullRequest",
                                          "repo": repo, "pr": pr}}
        if name == "mergePullRequest":
            inp = args.get("input") or {}
            parsed = parse_gid(inp.get("pullRequestId"))
            if parsed is None or parsed[0] != "P":
                raise ValueError("unknown pull request")
            repo, pr = _gql_find_pr(parsed[1])
            if pr is None:
                raise ValueError("unknown pull request")
            _gql_squash_merge(repo, pr)
            return True, {"__type": "MergePullRequestPayload",
                          "pullRequest": {"__type": "PullRequest",
                                          "repo": repo, "pr": pr}}
        if name == "addComment":
            inp = args.get("input") or {}
            parsed = parse_gid(inp.get("subjectId"))
            if parsed is None or parsed[0] != "I":
                raise ValueError("unknown issue")
            repo, iss = _gql_find_issue(parsed[1])
            if iss is None:
                raise ValueError("unknown issue")
            with STORE.lock:
                cid = STORE.next_comment_id
                STORE.next_comment_id += 1
            comment = {"id": cid, "body": inp.get("body") or "",
                       "node_id": gid("C", cid)}
            iss["comments"].append(comment)
            return True, {"__type": "AddCommentPayload",
                          "commentEdge": {"__type": "CommentEdge",
                                          "node": {"__type": "Comment",
                                                   "comment": comment}}}
        return False, None
    if type_name == "Repository":
        repo = value["repo"]
        if name == "id":
            return True, repo.global_id()
        if name == "databaseId":
            return True, repo.db_id
        if name == "name":
            return True, repo.name
        if name == "nameWithOwner":
            return True, "%s/%s" % (repo.owner, repo.name)
        if name == "owner":
            return True, {"__type": "User", "login": repo.owner}
        if name == "defaultBranchRef":
            tip = repo.tip()
            if tip is None:
                return True, None
            return True, {"__type": "Ref", "name": repo.default_branch}
        if name == "viewerPermission":
            return True, "ADMIN"
        if name == "hasIssuesEnabled":
            return True, True
        if name == "description":
            return True, repo.description
        if name == "isPrivate":
            return True, repo.private
        if name == "createdAt" or name == "updatedAt":
            return True, FIXED_TS
        if name == "url":
            return True, "https://example.invalid/%s/%s" % (repo.owner, repo.name)
        if name == "issue":
            iss = repo.issues.get(args.get("number"))
            if iss is None:
                return True, None
            return True, {"__type": "Issue", "repo": repo, "issue": iss}
        if name == "issues":
            return True, _gql_issues_conn(repo, args)
        if name == "pullRequests":
            items = [p for p in sorted(repo.pulls.values(),
                                       key=lambda p: p["number"])
                     if p["state"] == "open"]
            first = args.get("first")
            if isinstance(first, int):
                items = items[:first]
            return True, {"__type": "PullRequestConnection",
                          "nodes": [{"__type": "PullRequest", "repo": repo,
                                     "pr": p} for p in items]}
        if name == "pullRequest":
            pr = repo.pulls.get(args.get("number"))
            if pr is None:
                return True, None
            return True, {"__type": "PullRequest", "repo": repo, "pr": pr}
        return False, None
    if type_name == "Issue":
        iss = value["issue"]
        if name == "id":
            return True, gid("I", iss["id"])
        if name == "number":
            return True, iss["number"]
        if name == "title":
            return True, iss["title"]
        if name == "state":
            return True, iss["state"].upper()
        if name == "body":
            return True, iss["body"]
        if name == "createdAt" or name == "updatedAt":
            return True, FIXED_TS
        if name == "url":
            return True, "https://example.invalid/%s/%s/issues/%d" % (
                value["repo"].owner, value["repo"].name, iss["number"])
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
            return True, ("MERGEABLE" if h._merge_conflict(repo, pr) is None
                          else "CONFLICTING")
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
        if name == "hasNextPage":
            return True, value.get("hasNextPage", False)
        if name == "endCursor":
            return True, value.get("endCursor")
        if name == "hasPreviousPage":
            return True, False
        if name == "startCursor":
            return True, None
        return False, None
    if type_name == "PullRequestChangedFile":
        if name == "path":
            return True, value["path"]
        return False, None
    if type_name == "User":
        if name == "login":
            return True, value["login"]
        if name == "id":
            return True, gid("U", VIEWER_ID)
        if name == "name":
            return True, VIEWER_NAME
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


def _gql_exec_value(h, result, selection, variables, errors):
    if result is None:
        return None
    if isinstance(result, list):
        return [_gql_exec_value(h, item, selection, variables, errors)
                for item in result]
    if isinstance(result, dict) and "__type" in result:
        if selection:
            out, errs = _gql_exec_selection(
                h, selection, result["__type"], result, variables)
            errors.extend(errs)
            return out
        return {}
    return result


def _gql_exec_selection(h, selection, type_name, value, variables):
    out = {}
    errors = []
    for field in selection:
        if field.get("fragment"):
            if field["type"] is None or field["type"] == type_name:
                sub, errs = _gql_exec_selection(
                    h, field["selection"], type_name, value, variables)
                out.update(sub)
                errors.extend(errs)
            continue
        fname = field["name"]
        alias = field.get("alias") or fname
        args = {k: _gql_resolve(v, variables)
                for k, v in field["args"].items()}
        try:
            found, result = _gql_resolve_field(h, type_name, value, fname, args)
        except Exception as exc:
            errors.append({"message": str(exc)})
            out[alias] = None
            continue
        if not found:
            errors.append({"message": 'Cannot query field "%s" on type "%s".'
                           % (fname, type_name)})
            continue
        out[alias] = _gql_exec_value(h, result, field.get("selection"),
                                     variables, errors)
    return out, errors


def _gql_exec_one(h, op):
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
    data, errors = _gql_exec_selection(h, chosen["selection"], root, None,
                                       variables)
    result = {"data": data}
    if errors:
        result["errors"] = errors
    return result


def _graphql(h):
    if not h.authed():
        h.send_error_json(401, "Requires authentication")
        return
    body, err = h.read_json_body()
    if err:
        h.send_error_json(400, err)
        return
    if isinstance(body, list):
        out = [_gql_exec_one(h, op) for op in body]
    elif isinstance(body, dict):
        out = _gql_exec_one(h, body)
    else:
        h.send_error_json(400, "Invalid JSON")
        return
    h.send_json(200, out)


Handler._graphql = _graphql

# --------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 Streamable HTTP)
# --------------------------------------------------------------------------

_MCP_TOOLS = [
    {"name": "create_repo",
     "description": "Create a new repository for the authenticated user.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "description": {"type": "string"},
         "private": {"type": "boolean"}}, "required": ["name"]}},
    {"name": "get_repo",
     "description": "Get a repository by owner and name.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"}},
         "required": ["owner", "repo"]}},
    {"name": "create_issue",
     "description": "Create an issue in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string"}, "body": {"type": "string"}},
         "required": ["owner", "repo", "title"]}},
    {"name": "list_issues",
     "description": "List issues in a repository.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "state": {"type": "string"}},
         "required": ["owner", "repo"]}},
    {"name": "create_pull_request",
     "description": "Create a pull request between two branches.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "title": {"type": "string"}, "head": {"type": "string"},
         "base": {"type": "string"}, "body": {"type": "string"}},
         "required": ["owner", "repo", "title", "head", "base"]}},
    {"name": "merge_pull_request",
     "description": "Merge an open pull request with a squash merge.",
     "inputSchema": {"type": "object", "properties": {
         "owner": {"type": "string"}, "repo": {"type": "string"},
         "pull_number": {"type": "integer"}},
         "required": ["owner", "repo", "pull_number"]}},
]


def _mcp_norm(args):
    """Normalize tool arguments by synonym: accept owner/repo, name,
    full_name, title, body, head, base, pull_number, state, description,
    private, and their common aliases."""
    out = {}
    if not isinstance(args, dict):
        return out
    for key, val in args.items():
        lk = str(key).lower()
        if lk in ("owner", "username", "user"):
            out.setdefault("owner", val)
        elif lk in ("repo", "repository", "repository_name", "repo_name"):
            out.setdefault("repo", val)
        elif lk == "full_name" and isinstance(val, str) and "/" in val:
            o, r = val.split("/", 1)
            out.setdefault("owner", o)
            out.setdefault("repo", r)
        elif lk in ("title", "name"):
            out.setdefault("title", val)
            out.setdefault("name", val)
        elif lk == "body":
            out["body"] = val
        elif lk in ("head", "head_branch", "head_ref", "headrefname"):
            out["head"] = val
        elif lk in ("base", "base_branch", "base_ref", "baserefname"):
            out["base"] = val
        elif lk in ("pull_number", "number", "pr_number", "pullnumber"):
            out["pull_number"] = val
        elif lk == "state":
            out["state"] = val
        elif lk == "description":
            out["description"] = val
        elif lk == "private":
            out["private"] = val
        elif lk == "id":
            out["id"] = val
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


def _mcp_call(h, name, raw_args):
    args = _mcp_norm(raw_args)
    if name == "create_repo":
        missing = _mcp_missing(["name"], args)
        if missing:
            return missing, None
        name = args["name"]
        if STORE.get_repo(VIEWER_LOGIN, name) is not None:
            return _mcp_error_result("Repository already exists"), None
        repo = STORE.create_repo(name, args.get("description") or "",
                                 bool(args.get("private")))
        return _mcp_text_result(_repo_json(repo)), None
    if name == "get_repo":
        missing = _mcp_missing(["owner", "repo"], args)
        if missing:
            return missing, None
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None or args["owner"] != VIEWER_LOGIN:
            return _mcp_error_result("Repository not found"), None
        return _mcp_text_result(_repo_json(repo)), None
    if name == "create_issue":
        missing = _mcp_missing(["owner", "repo", "title"], args)
        if missing:
            return missing, None
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None or args["owner"] != VIEWER_LOGIN:
            return _mcp_error_result("Repository not found"), None
        iss = _gql_create_issue(repo, args["title"], args.get("body") or "")
        return _mcp_text_result(h._issue_json(repo, iss)), None
    if name == "list_issues":
        missing = _mcp_missing(["owner", "repo"], args)
        if missing:
            return missing, None
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None or args["owner"] != VIEWER_LOGIN:
            return _mcp_error_result("Repository not found"), None
        state = str(args.get("state") or "open").lower()
        items = [h._issue_json(repo, i) for i in repo.issues.values()
                 if state == "all" or i["state"] == state]
        items.sort(key=lambda x: x["number"])
        return _mcp_text_result({"issues": items}), None
    if name == "create_pull_request":
        missing = _mcp_missing(["owner", "repo", "title", "head", "base"], args)
        if missing:
            return missing, None
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None or args["owner"] != VIEWER_LOGIN:
            return _mcp_error_result("Repository not found"), None
        try:
            pr = _gql_create_pull(repo, args["title"], args["head"],
                                  args["base"])
        except ValueError as exc:
            return _mcp_error_result(str(exc)), None
        return _mcp_text_result(h._pr_json(repo, pr)), None
    if name == "merge_pull_request":
        missing = _mcp_missing(["owner", "repo", "pull_number"], args)
        if missing:
            return missing, None
        repo = STORE.get_repo(args["owner"], args["repo"])
        if repo is None or args["owner"] != VIEWER_LOGIN:
            return _mcp_error_result("Repository not found"), None
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
    return None, {"code": -32602, "message": "Unknown tool: %s" % name}


def _mcp_dispatch(h, method, params):
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
        return _mcp_call(h, params.get("name"), params.get("arguments") or {})
    return None, {"code": -32601, "message": "Method not found: %s" % method}


def _mcp(h):
    if not h.authed():
        h.send_error_json(401, "Requires authentication")
        return
    raw = h.read_body()
    try:
        req = json.loads(raw.decode("utf-8"))
    except Exception:
        h._send(400, json_bytes({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": "Parse error"}}))
        return
    if not isinstance(req, dict):
        h._send(400, json_bytes({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700,
                                           "message": "Parse error"}}))
        return
    method = req.get("method")
    req_id = req.get("id")
    if req_id is None:
        # notification (e.g. notifications/initialized): 202, empty body
        h._send(202, b"")
        return
    result, error = _mcp_dispatch(h, method, req.get("params") or {})
    if error is not None:
        payload = {"jsonrpc": "2.0", "id": req_id, "error": error}
    else:
        payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    h.send_json(200, payload)


Handler._mcp = _mcp

# --------------------------------------------------------------------------
# Git smart HTTP
# --------------------------------------------------------------------------

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


def _git_info_refs(h, repo, service):
    v2 = (service == "git-upload-pack"
          and h.headers.get("Git-Protocol") == "version=2")
    if v2:
        body = (pkt(b"version 2\n")
                + pkt(b"ls-refs=unborn\n")
                + pkt(b"fetch=shallow filter\n")
                + pkt(b"object-format=sha1\n")
                + pkt(("agent=%s\n" % AGENT).encode())
                + pkt_flush())
        h._send(200, body, content_type="application/x-git-upload-pack-"
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
    out = pkt(("# service=%s\n" % service).encode()) + pkt_flush()
    if not lines:
        out += pkt(("%s capabilities^{}\x00%s\n" % (ZERO_OID, caps)).encode())
    else:
        out += pkt(("%s\x00%s\n" % (head, caps)).encode())
        for line in lines:
            out += pkt(line.encode() + b"\n")
    out += pkt_flush()
    h._send(200, out, content_type=ctype)


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
        otype, data = obj
        if otype == "commit":
            parsed = parse_commit(data)
            stack.append(parsed["tree"])
            if oid not in shallow_set:
                stack.extend(parsed["parents"])
        elif otype == "tag":
            stack.append(parse_tag(data)["object"])
        elif otype == "tree":
            for _mode, _name, child in parse_tree(data):
                stack.append(child)
    return seen


def _git_fetch_objects(repo, wants, haves, depth, old_shallow):
    """Objects to send for a fetch, plus shallow/unshallow boundary lists."""
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
    # BFS distances from the wants (0 = want itself, 1 = parent, ...)
    dist = {}
    queue = [(c, 0) for c in want_commits]
    qi = 0
    while qi < len(queue):
        commit, d = queue[qi]
        qi += 1
        if commit in dist:
            continue
        dist[commit] = d
        obj = repo.get_object(commit)
        if obj is None or obj[0] != "commit":
            continue
        for parent in parse_commit(obj[1])["parents"]:
            if parent not in dist:
                queue.append((parent, d + 1))
    if depth is None:
        included = set(dist)
    else:
        included = {c for c, d in dist.items() if d < depth}
    have_closure = _have_closure(repo, haves, old_shallow)
    send = {}
    for oid, obj in extra.items():
        if oid not in have_closure:
            send[oid] = obj
    for commit in included:
        if commit in have_closure:
            continue
        obj = repo.get_object(commit)
        if obj is None:
            continue
        send[commit] = obj
        tree = parse_commit(obj[1])["tree"]
        for oid in reachable(repo, [tree]):
            if oid not in have_closure and oid not in send:
                send[oid] = repo.get_object(oid)
    shallow = []
    unshallow = []
    if depth is not None:
        for commit, d in dist.items():
            if d == depth - 1 and commit in included:
                obj = repo.get_object(commit)
                if obj is not None and obj[0] == "commit":
                    if parse_commit(obj[1])["parents"]:
                        shallow.append(commit)
        for boundary in (old_shallow or []):
            obj = repo.get_object(boundary)
            if obj is None or obj[0] != "commit":
                continue
            if any(p in send
                   for p in parse_commit(obj[1])["parents"]):
                unshallow.append(boundary)
    return send, sorted(shallow), sorted(unshallow)


def _git_sideband_pack(pack):
    out = bytearray()
    for off in range(0, len(pack), 32768):
        chunk = pack[off:off + 32768]
        out += pkt(b"\x01" + chunk)
    return bytes(out)


def _git_upload_pack_v2(h, repo, lines):
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
        out = bytearray()
        tip = repo.tip()
        if not prefixes:
            if tip is not None:
                out += pkt(("%s HEAD symref-target:refs/heads/main\n" % tip).encode())
            else:
                out += pkt(b"unborn HEAD symref-target:refs/heads/main\n")
        for name, oid in sorted(repo.refs.items()):
            if prefixes and not any(name.startswith(p) for p in prefixes):
                continue
            line = ("%s %s" % (oid, name)).encode()
            if symrefs and name == "refs/heads/" + repo.default_branch:
                pass
            if peel:
                obj = repo.get_object(oid)
                if obj is not None and obj[0] == "tag":
                    commit = peel_to_commit(repo, oid)
                    if commit is not None:
                        line += (" peeled:%s" % commit).encode()
            out += pkt(line + b"\n")
        out += pkt_flush()
        h._send(200, bytes(out),
                content_type="application/x-git-upload-pack-result")
        return
    if command == "fetch":
        send, shallow, unshallow = _git_fetch_objects(
            repo, wants, haves, depth, old_shallow)
        out = bytearray()
        out += pkt(b"acknowledgments\n") + pkt(b"NAK\n") + pkt_delim()
        if depth is not None or old_shallow:
            out += pkt(b"shallow-info\n")
            for oid in shallow:
                out += pkt(("shallow %s\n" % oid).encode())
            for oid in unshallow:
                out += pkt(("unshallow %s\n" % oid).encode())
            out += pkt_delim()
        if send:
            items = sorted(send.items())
            pack = write_pack([obj for _oid, obj in items])
            out += pkt(b"packfile\n")
            out += _git_sideband_pack(pack)
        out += pkt_flush()
        h._send(200, bytes(out),
                content_type="application/x-git-upload-pack-result")
        return
    raise ValueError("unknown v2 command: %s" % command)


def _git_upload_pack(h, repo):
    body = h.read_body()
    v2 = h.headers.get("Git-Protocol") == "version=2"
    try:
        packets, _off = parse_pkt_lines(body)
    except ValueError:
        h._send(400, b"bad pkt-line", content_type="text/plain")
        return
    lines = [payload.decode("utf-8", "replace").rstrip("\n")
             for kind, payload in packets if kind == "data"]
    if v2:
        _git_upload_pack_v2(h, repo, lines)
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
    send, shallow, unshallow = _git_fetch_objects(
        repo, wants, haves, depth, old_shallow)
    out = bytearray()
    out += pkt(b"NAK\n")
    for oid in shallow:
        out += pkt(("shallow %s\n" % oid).encode())
    for oid in unshallow:
        out += pkt(("unshallow %s\n" % oid).encode())
    if send:
        items = sorted(send.items())
        pack = write_pack([obj for _oid, obj in items])
        out += _git_sideband_pack(pack)
    out += pkt_flush()
    h._send(200, bytes(out),
            content_type="application/x-git-upload-pack-result")


def _git_receive_pack(h, repo):
    body = h.read_body()
    try:
        packets, off = parse_pkt_lines(body)
    except ValueError:
        h._send(400, b"bad pkt-line", content_type="text/plain")
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
            _git_send_report(h, report)
            return
    report.append("unpack ok")
    for old, new, ref in commands:
        if not ref.startswith("refs/"):
            report.append("ng %s invalid ref" % ref)
            continue
        current = repo.refs.get(ref)
        if new == ZERO_OID:
            if current is None:
                report.append("ng %s no such ref" % ref)
            else:
                del repo.refs[ref]
                report.append("ok %s" % ref)
            continue
        if old == ZERO_OID:
            if current is not None:
                report.append("ng %s already exists" % ref)
            elif repo.get_object(new) is None:
                report.append("ng %s object not found" % ref)
            else:
                repo.refs[ref] = new
                report.append("ok %s" % ref)
            continue
        if current is None:
            report.append("ng %s no such ref" % ref)
        elif current != old:
            report.append("ng %s non-fast-forward" % ref)
        elif repo.get_object(new) is None:
            report.append("ng %s object not found" % ref)
        elif new == old:
            report.append("ok %s" % ref)
        elif not is_ancestor(repo, old, new):
            report.append("ng %s non-fast-forward" % ref)
        else:
            repo.refs[ref] = new
            report.append("ok %s" % ref)
    _git_send_report(h, report)


def _git_send_report(h, lines):
    data = b"".join(pkt((line + "\n").encode()) for line in lines)
    out = bytearray()
    for off in range(0, len(data), 32768):
        out += pkt(b"\x01" + data[off:off + 32768])
    out += pkt_flush()
    h._send(200, bytes(out),
            content_type="application/x-git-receive-pack-result")


def _git(h, method, owner, repo_name, rest, query):
    if not h.authed():
        h.send_error_json(401, "Requires authentication")
        return
    repo = STORE.get_repo(owner, repo_name)
    try:
        if rest == "/info/refs":
            service = query.get("service")
            if repo is None:
                h.send_error_json(404, "Not Found")
                return
            if service not in ("git-upload-pack", "git-receive-pack"):
                h.send_error_json(400, "Unknown service")
                return
            _git_info_refs(h, repo, service)
        elif rest == "/git-upload-pack" and method == "POST":
            if repo is None:
                h.send_error_json(404, "Not Found")
                return
            _git_upload_pack(h, repo)
        elif rest == "/git-receive-pack" and method == "POST":
            if repo is None:
                h.send_error_json(404, "Not Found")
                return
            _git_receive_pack(h, repo)
        else:
            h.send_error_json(404, "Not Found")
    except Exception as exc:
        try:
            h._send(400, ("bad request: %s" % exc).encode(),
                    content_type="text/plain")
        except Exception:
            pass


Handler._git = _git

# --------------------------------------------------------------------------
# Server bootstrap + env lifecycle
# --------------------------------------------------------------------------


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


def _http_post_json(url, payload, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _http_get_json(url, timeout=30):
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _register(port):
    url = ENV_URL.rstrip("/") + "/register"
    delay = 0.25
    while True:
        try:
            status, _body = _http_post_json(url, {"port": port})
            if status in (200, 201, 409):
                return
            raise RuntimeError("register returned %d" % status)
        except Exception as exc:
            if "register returned 4" in str(exc):
                raise
            delay = min(delay * 2, 2.0)
            time.sleep(delay)


def _wait_done():
    url = ENV_URL.rstrip("/") + "/done"
    delay = 0.25
    while True:
        try:
            status, body = _http_get_json(url)
            if status == 200:
                try:
                    if json.loads(body.decode("utf-8")).get("done"):
                        return
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass
        delay = min(delay * 2, 2.0)
        time.sleep(delay)


def main():
    if not ENV_URL:
        port = int(os.environ.get("PORT", "8765"))
        srv = Server(("127.0.0.1", port), Handler)
        print("listening on port %d (local mode, no MAKEFUN_ENV_URL)"
              % port, flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    srv = Server(("0.0.0.0", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    port = srv.server_address[1]
    print("github-core-clone listening on port %d" % port, flush=True)
    _register(port)
    print("github-core-clone registered; waiting for the env to say done",
          flush=True)
    _wait_done()
    srv.shutdown()
    srv.server_close()
    print("github-core-clone done", flush=True)


if __name__ == "__main__":
    main()
