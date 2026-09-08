#!/usr/bin/env python3
"""
Durable local SQLite inbox/operation ledger for inbound Tdarr webhook events.

Compatibility identity:
  operation_key = sha256(normalized mapped absolute media path)
  - normalized: map_path() applied ( /data/... -> /mnt/nas/share/media/... ),
    then os.path.abspath(), stripped whitespace, case-sensitive (Linux).
  - tdarr _id is stored as metadata but NOT part of identity; file/_id is not
    assumed to be a globally unique event ID (Tdarr restarts re-fire webhooks
    for every file, and _id may be absent/unstable). See compute_operation_key().
  - If 'file' field is missing, empty, non-string, or fails normalization
    (e.g. no directory component or no recognized video extension when
    extension filtering is enabled), compute_operation_key raises ValueError and
    the caller must fail closed without starting a worker or creating a ledger row.
  - Source-generation conflicts: fingerprint (size/mtime_ns) captured at
    insert_received; a later delivery for the same operation_key with a
    different fingerprint is marked 'ambiguous' and the worker is not started.
    This prevents conflicting generations of the same path from racing.

States: received, claimed, processing, succeeded, no_op, failed_retryable,
        failed_final, ambiguous

Lease: claimed/processing rows carry lease_expires_at (unix ts) and lease_owner.
       Stale leases (lease_expires_at < now and status in claimed/processing)
       are recovered conservatively via recover_stale_leases(): only those
       two statuses are ever reset to 'received' (retryable); terminal states
       are never recovered. Callers should run recover_stale_leases before
       try_claim, or try_claim will refuse a stale-locked row until recovery.

Storage: SQLite file under the same local state directory as orchestrator's
         STATE_FILE (default ~/.config/asr-pipeline/webhook_inbox.db). Never NFS;
         init_ledger refuses paths under /mnt/nas or /media.
         Uses WAL mode and file locking via SQLite; callers use a per-call
         connection with timeout for concurrency.

Secrets: never logged; file paths are not secrets but API keys are masked
         by the caller before logging.
"""
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

VALID_STATUSES = ("received","claimed","processing","succeeded","no_op","failed_retryable","failed_final","ambiguous")
TERMINAL_STATUSES = ("succeeded","no_op","failed_final","ambiguous")

# default lease ttl seconds; conservative for long NFS extraction (ffmpeg timeout 1800)
DEFAULT_LEASE_TTL = 1800

_lock = threading.RLock()

# Allow tests to override via env or patch; otherwise derive from orchestrator.STATE_FILE
def _default_state_file():
    try:
        import orchestrator as o
        return o.STATE_FILE
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".config", "asr-pipeline", "state.jsonl")

def _default_db_path():
    sf = os.environ.get("STATE_FILE") or _default_state_file()
    d = os.path.dirname(os.path.abspath(sf)) or "."
    return os.path.join(d, "webhook_inbox.db")

# indirection for tests to patch
_DB_PATH_OVERRIDE = None

def get_db_path():
    if _DB_PATH_OVERRIDE is not None:
        return _DB_PATH_OVERRIDE
    # also respect env if tests set STATE_FILE after import
    # if orchestrator.STATE_FILE was patched, use its dirname
    try:
        import orchestrator as o
        sf = o.STATE_FILE
        d = os.path.dirname(os.path.abspath(sf)) or "."
        return os.path.join(d, "webhook_inbox.db")
    except Exception:
        return _default_db_path()

def _is_nfs_path(p):
    ap = os.path.abspath(p)
    return ap.startswith("/mnt/nas") or ap.startswith("/media/") or "/mnt/nas/" in ap

def _ensure_not_nfs(path):
    if _is_nfs_path(path):
        raise RuntimeError(f"ledger must be local, not NFS: {path}")

def _connect(db_path=None):
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=10000;")
    return con

def init_ledger(db_path=None):
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    with _lock:
        con = _connect(path)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_key TEXT UNIQUE NOT NULL,
                    file_path TEXT NOT NULL,
                    container_path TEXT NOT NULL,
                    tdarr_id TEXT,
                    status TEXT NOT NULL,
                    lease_expires_at INTEGER,
                    lease_owner TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT,
                    fingerprint_json TEXT
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_ops_status ON operations(status)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_ops_lease ON operations(lease_expires_at)")
            # ensure WAL
            con.execute("PRAGMA journal_mode=WAL;")
        finally:
            con.close()
    return path

def _normalize_mapped_path(container_path):
    if not isinstance(container_path, str):
        raise ValueError("file must be a non-empty string")
    s = container_path.strip()
    if not s:
        raise ValueError("file must be non-empty")
    # basic sanity: must contain '/' and have a basename
    if "/" not in s:
        raise ValueError("file must be a path")
    # map via orchestrator.map_path if available (handles /data -> /mnt/nas)
    try:
        import orchestrator as o
        mapped = o.map_path(s)
    except Exception:
        # fallback: manual
        if s.startswith("/data/"):
            mapped = "/mnt/nas/share/media/" + s[len("/data/"):]
        else:
            mapped = s
    # normalize to absolute
    mapped = os.path.abspath(mapped)
    # extension check: require known video extensions; reject .txt.bak etc.
    # Allowlist mirrors embedded sweep: .mkv .mp4 .avi .ts .mov .m2ts
    # If no known extension, treat as invalid identity
    low = mapped.lower()
    allowed = (".mkv",".mp4",".avi",".ts",".mov",".m2ts",".webm")
    if not any(low.endswith(ext) for ext in allowed):
        raise ValueError(f"file must be a video path with extension {allowed}: {mapped}")
    return mapped

def compute_operation_key(container_path):
    """Return stable operation_key for a container_path, or raise ValueError."""
    mapped = _normalize_mapped_path(container_path)
    return hashlib.sha256(mapped.encode("utf-8")).hexdigest()

def _now():
    return int(time.time())

def insert_received(container_path, tdarr_id=None, fingerprint=None, db_path=None):
    """Insert received operation or return existing. Returns (op_dict, is_new)."""
    key = compute_operation_key(container_path)
    mapped = _normalize_mapped_path(container_path)
    fp_json = json.dumps(fingerprint, sort_keys=True) if fingerprint is not None else None
    now = _now()
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    with _lock:
        con = _connect(path)
        try:
            # check existing
            cur = con.execute("SELECT * FROM operations WHERE operation_key=?", (key,))
            row = cur.fetchone()
            if row is not None:
                # fingerprint conflict detection: if stored fingerprint exists and differs -> ambiguous
                if fingerprint is not None and fp_json is not None:
                    cur2 = con.execute("SELECT fingerprint_json, status FROM operations WHERE operation_key=?", (key,))
                    r = cur2.fetchone()
                    if r:
                        # column indices: fingerprint_json is 12th? Let's fetch by name instead
                        pass
                # fetch op dict
                op = get_operation(key, db_path=path)
                # check fingerprint conflict -> mark ambiguous, fail closed
                if fingerprint is not None and op.get("fingerprint_json"):
                    try:
                        stored = json.loads(op["fingerprint_json"]) if op["fingerprint_json"] else None
                    except Exception:
                        stored = None
                    if stored is not None and stored != fingerprint:
                        # ambiguous conflict
                        con.execute("UPDATE operations SET status=?, updated_at=?, last_error=? WHERE operation_key=?",
                                    ("ambiguous", now, "fingerprint conflict", key))
                        op = get_operation(key, db_path=path)
                        return op, False
                return op, False
            # insert new
            con.execute("INSERT INTO operations (operation_key, file_path, container_path, tdarr_id, status, attempt, created_at, updated_at, fingerprint_json) VALUES (?,?,?,?,?,?,?,?,?)",
                        (key, mapped, container_path, tdarr_id or "", "received", 0, now, now, fp_json))
            op = get_operation(key, db_path=path)
            return op, True
        finally:
            con.close()

def try_claim(operation_key, lease_ttl=DEFAULT_LEASE_TTL, db_path=None):
    """Atomically claim a received (or stale) operation. Returns ownership token string if claimed, else falsy."""
    now = _now()
    expires = now + int(lease_ttl)
    owner = str(uuid.uuid4())
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    with _lock:
        con = _connect(path)
        try:
            # atomic update: only claim if status is received
            # Use single UPDATE with condition; check changes
            cur = con.execute(
                "UPDATE operations SET status='claimed', lease_expires_at=?, lease_owner=?, attempt=attempt+1, updated_at=? WHERE operation_key=? AND status='received'",
                (expires, owner, now, operation_key)
            )
            if cur.rowcount == 1:
                return owner
            return None
        finally:
            con.close()

def set_status(operation_key, status, last_error=None, fingerprint=None, db_path=None, expected_owner=None):
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status}")
    now = _now()
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    with _lock:
        con = _connect(path)
        try:
            # fenced update: if expected_owner provided, only update if lease_owner matches
            if expected_owner is not None:
                cur = con.execute("SELECT lease_owner, status, lease_expires_at FROM operations WHERE operation_key=?", (operation_key,))
                row = cur.fetchone()
                if row is None:
                    return False
                current_owner, current_status, current_expires = row[0], row[1], row[2]
                if current_owner != expected_owner:
                    return False
                # for terminal transitions, require processing status and non-expired lease
                if status in TERMINAL_STATUSES or status in ("succeeded","no_op","failed_retryable","failed_final","ambiguous"):
                    # only allow terminal from processing (not claimed, not already terminal)
                    if current_status != "processing":
                        return False
                    if current_expires is not None:
                        try:
                            if int(now) > int(current_expires):
                                return False
                        except Exception:
                            return False
            # clear lease when moving to terminal or succeeded/no_op, keep attempt
            if status in TERMINAL_STATUSES or status in ("succeeded","no_op","failed_retryable","failed_final","ambiguous"):
                # for retryable we clear lease so it can be reclaimed after recovery? Actually claimed->processing->failed_retryable clears lease
                lease_clause = ", lease_expires_at=NULL, lease_owner=NULL"
            elif status in ("received",):
                lease_clause = ", lease_expires_at=NULL, lease_owner=NULL"
            else:
                # claimed/processing keep lease
                lease_clause = ""
            fp_json = json.dumps(fingerprint, sort_keys=True) if fingerprint is not None else None
            if expected_owner is not None:
                # fenced status transition
                if fingerprint is not None:
                    cur = con.execute(f"UPDATE operations SET status=?, updated_at=?, last_error=?{lease_clause}, fingerprint_json=? WHERE operation_key=? AND lease_owner=?",
                                (status, now, last_error, fp_json, operation_key, expected_owner))
                else:
                    cur = con.execute(f"UPDATE operations SET status=?, updated_at=?, last_error=?{lease_clause} WHERE operation_key=? AND lease_owner=?",
                                (status, now, last_error, operation_key, expected_owner))
                if cur.rowcount == 0:
                    return False
            else:
                if fingerprint is not None:
                    con.execute(f"UPDATE operations SET status=?, updated_at=?, last_error=?{lease_clause}, fingerprint_json=? WHERE operation_key=?",
                                (status, now, last_error, fp_json, operation_key))
                else:
                    con.execute(f"UPDATE operations SET status=?, updated_at=?, last_error=?{lease_clause} WHERE operation_key=?",
                                (status, now, last_error, operation_key))
            # lease handling for processing: extend lease conservatively, preserve owner if fenced
            if status == "processing":
                expires = now + DEFAULT_LEASE_TTL
                if expected_owner is not None:
                    # keep same owner, just extend
                    con.execute("UPDATE operations SET lease_expires_at=? WHERE operation_key=? AND lease_owner=?", (expires, operation_key, expected_owner))
                    # verify still owned
                    cur2 = con.execute("SELECT changes()")
                    # fallback: check rowcount by SELECT; instead rely on earlier check
                else:
                    # unfenced path: try to preserve existing owner if present, else create one
                    cur_o = con.execute("SELECT lease_owner FROM operations WHERE operation_key=?", (operation_key,))
                    r = cur_o.fetchone()
                    if r and r[0]:
                        con.execute("UPDATE operations SET lease_expires_at=? WHERE operation_key=?", (expires, operation_key))
                    else:
                        owner = str(uuid.uuid4())
                        con.execute("UPDATE operations SET lease_expires_at=?, lease_owner=? WHERE operation_key=?", (expires, owner, operation_key))
            elif status == "claimed":
                # already set in try_claim
                pass
            return con.total_changes > 0
        finally:
            con.close()

def heartbeat(operation_key, token, lease_ttl=DEFAULT_LEASE_TTL, db_path=None):
    """Renew lease for current owner. Returns True if lease extended, False if not owner or expired."""
    if not token:
        return False
    now = _now()
    expires = now + int(lease_ttl)
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    with _lock:
        con = _connect(path)
        try:
            cur = con.execute(
                "UPDATE operations SET lease_expires_at=?, updated_at=? WHERE operation_key=? AND lease_owner=? AND status IN ('claimed','processing')",
                (expires, now, operation_key, token)
            )
            return cur.rowcount == 1
        finally:
            con.close()

def _is_lease_expired(op):
    """Check if lease is expired (lease_expires_at < now). None means not expired."""
    try:
        exp = op.get("lease_expires_at")
        if exp is None:
            return False
        return int(time.time()) > int(exp)
    except Exception:
        return False

def finalize_operation(operation_key, token, status, last_error=None, db_path=None):
    """Fenced finalization: only succeeds if token matches, lease not expired, and status==processing.

    Rejects expired leases even if lease_owner matches and requires current status to be 'processing'
    before allowing any terminal transition. This prevents a stale owner that lost its lease from
    marking success after another owner has taken over.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"invalid status {status}")
    if not token:
        return False
    # terminal finalization requires processing status and non-expired lease
    # fetch current op to enforce expiry + processing check fail-closed
    try:
        op = get_operation(operation_key, db_path=db_path)
    except Exception:
        return False
    if not op:
        return False
    if op.get("lease_owner") != token:
        return False
    if op.get("status") != "processing":
        return False
    if _is_lease_expired(op):
        return False
    return set_status(operation_key, status, last_error=last_error, db_path=db_path, expected_owner=token)

def get_operation(operation_key, db_path=None):
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    con = _connect(path)
    try:
        cur = con.execute("SELECT operation_key, file_path, container_path, tdarr_id, status, lease_expires_at, lease_owner, attempt, created_at, updated_at, last_error, fingerprint_json FROM operations WHERE operation_key=?", (operation_key,))
        row = cur.fetchone()
        if row is None:
            return None
        cols = ["operation_key","file_path","container_path","tdarr_id","status","lease_expires_at","lease_owner","attempt","created_at","updated_at","last_error","fingerprint_json"]
        return dict(zip(cols, row))
    finally:
        con.close()

def list_operations(status=None, limit=100, db_path=None):
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    con = _connect(path)
    try:
        if status:
            cur = con.execute("SELECT operation_key, file_path, container_path, tdarr_id, status, lease_expires_at, lease_owner, attempt, created_at, updated_at, last_error, fingerprint_json FROM operations WHERE status=? ORDER BY updated_at DESC LIMIT ?", (status, limit))
        else:
            cur = con.execute("SELECT operation_key, file_path, container_path, tdarr_id, status, lease_expires_at, lease_owner, attempt, created_at, updated_at, last_error, fingerprint_json FROM operations ORDER BY updated_at DESC LIMIT ?", (limit,))
        cols = ["operation_key","file_path","container_path","tdarr_id","status","lease_expires_at","lease_owner","attempt","created_at","updated_at","last_error","fingerprint_json"]
        out = []
        for row in cur.fetchall():
            out.append(dict(zip(cols, row)))
        return out
    finally:
        con.close()

def recover_stale_leases(now=None, lease_ttl=DEFAULT_LEASE_TTL, db_path=None):
    """Recover stale claimed/processing leases whose lease_expires_at < now.
       Only those two statuses are recoverable; terminal are left alone.
       Returns number recovered (set to received)."""
    if now is None:
        now = _now()
    path = db_path or get_db_path()
    _ensure_not_nfs(path)
    init_ledger(path)
    with _lock:
        con = _connect(path)
        try:
            cur = con.execute(
                "UPDATE operations SET status='received', lease_expires_at=NULL, lease_owner=NULL, updated_at=? WHERE status IN ('claimed','processing') AND lease_expires_at IS NOT NULL AND lease_expires_at < ?",
                (now, now)
            )
            return cur.rowcount
        finally:
            con.close()

def _force_expire(operation_key, db_path=None):
    """Test helper: force lease to expired."""
    path = db_path or get_db_path()
    con = _connect(path)
    try:
        con.execute("UPDATE operations SET lease_expires_at=? WHERE operation_key=?", (1, operation_key))
    finally:
        con.close()

def count_by_status(db_path=None):
    path = db_path or get_db_path()
    init_ledger(path)
    con = _connect(path)
    try:
        cur = con.execute("SELECT status, COUNT(*) FROM operations GROUP BY status")
        return dict(cur.fetchall())
    finally:
        con.close()

def attempt_tmp_path(out_path, attempt):
    """Return unique attempt-specific tmp path for an extraction."""
    # sanitize attempt
    try:
        att = int(attempt)
    except Exception:
        att = 0
    uid = uuid.uuid4().hex[:8]
    return f"{out_path}.tmp.{att}.{uid}"

