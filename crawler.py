import json
import time
import random
import atexit
import argparse
import signal
import threading
from contextlib import contextmanager

import redis
import requests
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import execute_values
from psycopg2 import errors as pg_errors
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

# CONFIG
DB_CONFIG = {
    "dbname":   "crawler_test",
    "user":     "crawler_user",
    "password": "test123",
    "host":     "localhost",
    "port":     "5432",
}

REDIS_HOST    = "localhost"
REDIS_PORT    = 6379
REDIS_DB      = 0
REDIS_QUEUE   = "paper_queue"

BLOOM_NAME    = "processed_bloom"
BLOOM_CAP     = 100_000_000
BLOOM_FP_RATE = 0.000001

API_ROOT           = "https://api.semanticscholar.org/graph/v1"
API_BATCH_LIMIT    = 500
REQUEST_TIMEOUT    = 30
PAGE_LIMIT_DEFAULT = 1000
MAX_FAILED_PAGES   = 3
MAX_REQUEST_ATTEMPTS = 5

API_KEY            = ""

BATCH_SIZE  = API_BATCH_LIMIT

MAX_INSERT_RETRIES = 3
INSERT_BACKOFF     = 1.0
MAX_MARK_RETRIES   = MAX_INSERT_RETRIES

DESIRED_FIELDS = {
    "Computer Science", "Engineering", "Environmental Science",
    "Physics", "Mathematics", "Economics", "Business",
}

# PostgreSQL connection pool
DB_POOL = SimpleConnectionPool(minconn=1, maxconn=10, **DB_CONFIG)
atexit.register(DB_POOL.closeall)

@contextmanager
def get_db():
    conn = DB_POOL.getconn()
    try:
        cur = conn.cursor()
        yield conn, cur
        conn.commit()
    finally:
        cur.close()
        DB_POOL.putconn(conn)

# Redis + Bloom filter
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)
try:
    r.execute_command("BF.RESERVE", BLOOM_NAME, BLOOM_FP_RATE, BLOOM_CAP)
except redis.ResponseError as e:
    if "exists" not in str(e).lower():
        raise

# Graceful shutdown setup
shutdown_event = threading.Event()
def _handle_signal(signum, frame):
    shutdown_event.set()
signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# Rate‐limited request with retry logic
@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(MAX_REQUEST_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True
)
def send_request(sess, method, url, **kwargs):
    resp = sess.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
    if resp.status_code == 429:
        raise requests.RequestException("429 Too Many Requests")
    resp.raise_for_status()
    # Pause 1 second to respect 1 RPS
    time.sleep(1)
    return resp

def init_db(cur):
    cur.execute("""
      CREATE TABLE IF NOT EXISTS processed_papers (
        paper_id        TEXT PRIMARY KEY,
        fields_of_study TEXT[]
      );
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS citations (
        citing_id TEXT,
        cited_id  TEXT,
        PRIMARY KEY (citing_id, cited_id)
      );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cited   ON citations(cited_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_citing  ON citations(citing_id);")

# safe_insert_citations
def safe_insert_citations(cur, conn, rows):
    for attempt in range(1, MAX_INSERT_RETRIES + 1):
        try:
            execute_values(
                cur,
                "INSERT INTO citations (citing_id, cited_id) VALUES %s ON CONFLICT DO NOTHING",
                rows
            )
            conn.commit()
            return
        except pg_errors.DeadlockDetected:
            conn.rollback()
            if attempt == MAX_INSERT_RETRIES:
                raise
            time.sleep(INSERT_BACKOFF * (2 ** (attempt - 1)))

# mark_processed
def mark_processed(cur, conn, fos_map_for_ids):
    """
    fos_map_for_ids: dict mapping paper_id -> list_of_fields_of_study
    """
    if not fos_map_for_ids:
        return

    for attempt in range(1, MAX_MARK_RETRIES + 1):
        try:
            rows = []
            for pid, fos_list in fos_map_for_ids.items():
                rows.append((pid, fos_list))

            execute_values(
                cur,
                """
                INSERT INTO processed_papers (paper_id, fields_of_study)
                VALUES %s
                ON CONFLICT (paper_id) DO NOTHING
                """,
                rows,
                template="(%s, %s)"
            )
            conn.commit()

            pipe = r.pipeline()
            for pid in fos_map_for_ids.keys():
                pipe.execute_command("BF.ADD", BLOOM_NAME, pid)
            pipe.execute()
            return

        except pg_errors.DeadlockDetected:
            conn.rollback()
            if attempt == MAX_MARK_RETRIES:
                raise
            time.sleep(INSERT_BACKOFF * (2 ** (attempt - 1)))

# filter_new_ids
def filter_new_ids(cur, candidate_ids):
    if not candidate_ids:
        return []
    flags = r.execute_command("BF.MEXISTS", BLOOM_NAME, *candidate_ids)
    maybe_seen = [pid for pid, f in zip(candidate_ids, flags) if f]
    new_ids    = [pid for pid, f in zip(candidate_ids, flags) if not f]
    if maybe_seen:
        cur.execute(
            "SELECT paper_id FROM processed_papers WHERE paper_id = ANY(%s)",
            (maybe_seen,)
        )
        seen_sql = {row[0] for row in cur.fetchall()}
        for pid in seen_sql:
            r.execute_command("BF.ADD", BLOOM_NAME, pid)
        for pid in maybe_seen:
            if pid not in seen_sql:
                new_ids.append(pid)
    return new_ids

# fetch_references_paginated
def fetch_references_paginated(pid, sess, total):
    offset, refs, fails = 0, [], 0
    while offset < total:
        try:
            resp = send_request(
                sess, "GET",
                f"{API_ROOT}/paper/{pid}/references",
                params={
                    "fields": "reference.paperId",
                    "limit": PAGE_LIMIT_DEFAULT,
                    "offset": offset
                }
            )
            data = resp.json().get("data") or []
            if not data:
                break
            for d in data:
                rid = d.get("reference", {}).get("paperId")
                if rid:
                    refs.append(rid)
            offset += len(data)
        except Exception:
            fails += 1
            if fails >= MAX_FAILED_PAGES:
                break
            time.sleep((2 ** fails) + random.random())
    return refs

# chunked
def chunked(iterable, size):
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]

# main loop

def main(seeds=None, fresh=False, resume=False):
    sess = requests.Session()
    sess.headers.update({"x-api-key": API_KEY})

    with get_db() as (_, cur):
        init_db(cur)

    if fresh:
        r.delete(REDIS_QUEUE)
        with get_db() as (_, cur):
            cur.execute("TRUNCATE processed_papers, citations;")
        r.delete(BLOOM_NAME)
        r.execute_command("BF.RESERVE", BLOOM_NAME, BLOOM_FP_RATE, BLOOM_CAP)
        print("[INFO] Fresh start: cleared queue, SQL, Bloom")
    elif resume:
        ql = r.llen(REDIS_QUEUE)
        if ql == 0:
            print("[INFO] Resume with empty queue; use --fresh with seeds")
            return
        print(f"[INFO] Resuming crawl; queue length={ql}")

    if fresh and seeds:
        entries = [json.dumps({"id": pid, "depth": 0}) for pid in seeds]
        r.rpush(REDIS_QUEUE, *entries)
        print(f"[INFO] Seeded {len(seeds)} IDs; queue length={r.llen(REDIS_QUEUE)}")

    batch_count = total = 0
    while not shutdown_event.is_set():
        items = []
        for _ in range(BATCH_SIZE):
            ent = r.lpop(REDIS_QUEUE)
            if not ent:
                break
            items.append(ent)

        if not items:
            time.sleep(1)
            continue

        records = [json.loads(ent) for ent in items]
        pids     = [rec["id"] for rec in records if rec and rec.get("id")]
        depths   = {rec["id"]: rec.get("depth", 0) for rec in records if rec and rec.get("id")}
        current_batch = batch_count + 1
        print(f"[INFO] Batch {current_batch}: popped {len(pids)}/{BATCH_SIZE} ids")

        with get_db() as (conn, cur):
            # Deduplicate
            to_fetch = filter_new_ids(cur, pids)
            if not to_fetch:
                batch_count += 1
                continue

            # Combined metadata + references fetch
            batch_data = []
            for chunk in chunked(to_fetch, API_BATCH_LIMIT):
                resp = send_request(
                    sess, "POST",
                    f"{API_ROOT}/paper/batch?fields=paperId,referenceCount,fieldsOfStudy,references.paperId,references.fieldsOfStudy",
                    json={"ids": chunk}
                )
                batch_data.extend(resp.json() or [])

            # Parse and collect per-paper info
            counts, fos_map, ref_rows = {}, {}, []
            for rec in batch_data:
                if not rec:
                    continue
                pid0 = rec.get("paperId")
                if not pid0:
                    continue
                fields0 = rec.get("fieldsOfStudy") or []
                counts[pid0] = rec.get("referenceCount", 0)
                fos_map[pid0] = fields0

                if any(f in DESIRED_FIELDS for f in fields0):
                    for ref in rec.get("references") or []:
                        rid = ref.get("paperId")
                        if not rid:
                            continue
                        child_fields = ref.get("fieldsOfStudy") or []
                        if any(cf in DESIRED_FIELDS for cf in child_fields):
                            ref_rows.append((pid0, rid))

            # Heavy fallback for large-reference papers
            for pid0, cnt in counts.items():
                if cnt > PAGE_LIMIT_DEFAULT and any(f in DESIRED_FIELDS for f in fos_map.get(pid0, [])):
                    all_rids = set(fetch_references_paginated(pid0, sess, cnt))
                    if not all_rids:
                        continue

                    for chunk2 in chunked(list(all_rids), API_BATCH_LIMIT):
                        resp2 = send_request(
                            sess, "POST",
                            f"{API_ROOT}/paper/batch?fields=paperId,fieldsOfStudy",
                            json={"ids": chunk2}
                        )
                        data2 = resp2.json() or []
                        for child_rec in data2:
                            child_pid = child_rec.get("paperId")
                            if not child_pid:
                                continue
                            child_fields = child_rec.get("fieldsOfStudy") or []
                            if any(cf in DESIRED_FIELDS for cf in child_fields):
                                ref_rows.append((pid0, child_pid))

            print(f"[METRIC] Batch {current_batch}: papers fetched={len(to_fetch)}; new references discovered={len(ref_rows)}")

            # Insert citations
            safe_insert_citations(cur, conn, ref_rows)

            # Enqueue children FIFO
            new_entries = []
            for citing, cited in ref_rows:
                d = depths.get(citing, 0) + 1
                new_entries.append(json.dumps({"id": cited, "depth": d}))
            if new_entries:
                r.rpush(REDIS_QUEUE, *new_entries)

            # MARK PROCESSED
            fos_for_to_fetch = { pid: fos_map.get(pid, []) for pid in to_fetch }
            mark_processed(cur, conn, fos_for_to_fetch)

            batch_count += 1
            total += len(to_fetch)
            print(f"[INFO] Batch {current_batch} done; total fetched={total}; queue length={r.llen(REDIS_QUEUE)}")

    print("[INFO] Shutdown signal received, exiting.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fresh",  action="store_true")
    group.add_argument("--resume", action="store_true")
    parser.add_argument("seeds", nargs="*")
    args = parser.parse_args()
    main(seeds=args.seeds, fresh=args.fresh, resume=args.resume)
