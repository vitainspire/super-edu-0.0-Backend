"""Refresh the local textbook catalog from the publishing service.

Run after applying migration 0009, then on a schedule (nightly is fine) as
new books get published. Safe to re-run — it upserts and never discards
chapter prose already cached.

    venv/Scripts/python.exe scripts/sync_textbooks.py
"""
import sys
from dotenv import load_dotenv
load_dotenv()
from app.lib.textbook_catalog import sync_catalog

result = sync_catalog()
print(result)
sys.exit(0 if result.get("ok") else 1)
