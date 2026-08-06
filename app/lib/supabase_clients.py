import os
from supabase import create_client, Client

_admin_client: Client | None = None
_anon_client: Client | None = None


def create_admin_client() -> Client:
    """Server-side only — bypasses RLS. Mirrors backend/src/lib/supabase-admin.ts."""
    global _admin_client
    if _admin_client is None:
        url = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
        service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        _admin_client = create_client(url, service_role_key)
    return _admin_client


def get_anon_client() -> Client:
    """Used only to verify a bearer token via auth.get_user(token) — mirrors
    backend/src/lib/supabase-anon.ts."""
    global _anon_client
    if _anon_client is None:
        url = os.environ["NEXT_PUBLIC_SUPABASE_URL"]
        anon_key = os.environ["NEXT_PUBLIC_SUPABASE_ANON_KEY"]
        _anon_client = create_client(url, anon_key)
    return _anon_client
