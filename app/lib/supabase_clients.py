"""Supabase client factories.

THREAD-LOCAL, NOT PROCESS-WIDE, and that distinction is load-bearing.

These were plain module-level singletons: one client, created once, handed to
every caller. A `supabase.Client` wraps an `httpx` client, which owns one
HTTP/2 connection pool — and this codebase calls into it from several threads
at once. `prep_flow/tools.py` runs every Supabase lookup through
`asyncio.to_thread()` (it must: supabase-py is synchronous, and awaiting it on
the event loop would stall every other topic), and `context_assembly_node`
fans that out over four topics concurrently, each doing a canonical
resolution and a pedagogy lookup.

Four threads multiplexing one HTTP/2 connection is not something that pool
supports, and it does not fail cleanly. It corrupts the connection's frame
state, which surfaces as a rotating cast of socket errors that all look
transient and none of which name the real cause:

    [WinError 10035] A non-blocking socket operation could not be completed
    StreamIDTooLowError: 3 is lower than 7
    Invalid input StreamInputs.SEND_HEADERS in state 5
    Invalid input ConnectionInputs.RECV_DATA in state ConnectionState.CLOSED
    <ConnectionTerminated error_code:1, ...>

Every one of those was observed on real chapter runs, and each one silently
cost a topic its Pedagogy Library match or its canonical resolution — a
chapter came back with 5 of 12 activities "invented" because the library
lookup never completed, not because the library lacked a match.

A client per thread removes the sharing entirely. `asyncio.to_thread()` runs
on a bounded, REUSED ThreadPoolExecutor, so this creates a handful of clients
for the life of the process, not one per call — the pooling benefit is kept,
the cross-thread sharing is not.
"""
import os
import threading

from supabase import create_client, Client

# One slot per thread. Threads from asyncio's executor are reused, so each
# builds its client once and keeps it.
_local = threading.local()


def create_admin_client() -> Client:
    """Server-side only — bypasses RLS. Mirrors backend/src/lib/supabase-admin.ts."""
    client = getattr(_local, "admin_client", None)
    if client is None:
        url = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
        service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        client = create_client(url, service_role_key)
        _local.admin_client = client
    return client


def reset_admin_client() -> None:
    """Drop this thread's cached admin client so the next create_admin_client()
    builds a fresh one -- a fresh httpx connection, not the same pooled one.

    Exists for retry_supabase() below: a long-running job (a real chapter
    generation can run for minutes between DB touches, all the LLM calls in
    between) can hold this thread's one client, and its one connection, idle
    long enough for a network hop in between (a router, a load balancer, a
    firewall) to close it without telling either side. The next call then
    dies with something like `SSL: UNEXPECTED_EOF_WHILE_READING` -- a dead
    socket, not a real Supabase or auth problem. Recreating the client and
    trying once more is the fix; the old connection was never coming back."""
    _local.admin_client = None


def retry_supabase(build_and_run, attempts: int = 3):
    """Run a zero-arg callable that builds AND executes one Supabase query
    fresh each time (e.g. `lambda: create_admin_client().table(...).execute()`
    -- never close over an `ac` captured before the call, since a retry must
    see the client reset_admin_client() just dropped). Retries on ANY
    exception -- not just a fixed list of known network-error strings.

    That used to be narrower (a marker list: "SSL", "EOF", ...), on the theory
    that a genuine bug (a bad query, a constraint violation) shouldn't be
    silently retried. In practice that list needed patching twice in one day
    for two different real, transient connection errors it didn't recognize
    (an SSL EOF, then a plain WinError 10060 timeout) -- each one a caller
    losing the benefit of the retry until the string was added by hand. A
    genuine (non-network) error gains nothing from retrying, but loses
    nothing either: it fails the same way one call later, having cost one
    harmless extra attempt. And for save_published_chapter_lessons
    specifically, the caller this exists for, a failure here is never
    silently swallowed either way -- the backup file in
    var/chapter_save_backups/ (see prep_batch_jobs.py) means an unretryable
    failure still costs nothing, just a manual (free) recovery. Retrying
    broadly is strictly safer than maintaining a list that real production
    errors keep finding the edges of.

    THREE ATTEMPTS, NOT TWO. A single retry was the original fix, but a real
    production run (a teacher's own session, every /api/teacher/* endpoint)
    measured both the first attempt AND the one retry hitting the same SSL
    EOF back to back -- the connection was unstable for longer than one
    reset-and-try-again covers. A third attempt costs nothing when the first
    two already succeeded (the common case), and gives a sustained blip one
    more real chance before the caller gives up and the user sees a failure.

    Callers must only wrap a single query, not a block containing more than
    one write -- retrying re-runs the whole callable, and re-running an
    already-succeeded write alongside a failed one is exactly the bug this
    must not introduce."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return build_and_run()
        except Exception as e:
            last_exc = e
            print(f"[supabase] query failed (attempt {attempt}/{attempts}): {type(e).__name__}: {e}")
            if attempt < attempts:
                reset_admin_client()
    raise last_exc


def get_anon_client() -> Client:
    """Used only to verify a bearer token via auth.get_user(token) — mirrors
    backend/src/lib/supabase-anon.ts."""
    client = getattr(_local, "anon_client", None)
    if client is None:
        url = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
        anon_key = os.environ["NEXT_PUBLIC_SUPABASE_ANON_KEY"]
        client = create_client(url, anon_key)
        _local.anon_client = client
    return client


def reset_anon_client() -> None:
    """Drop this thread's cached anon client -- same dead-socket fix as
    reset_admin_client() above, for deps.py's require_user() retry, which
    runs on every /api/teacher/* and /api/admin/* request and so hits this
    thread-local client far more often than the admin one."""
    _local.anon_client = None
