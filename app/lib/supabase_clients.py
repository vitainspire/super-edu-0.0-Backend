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
