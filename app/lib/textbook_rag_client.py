"""Client for the external textbook-retrieval (RAG) service — semantic
search over ingested textbook content, for subtopic-level grounding that
textbook_grounding.py's own title-matching (best_match) can't do: it only
matches a topic/subtopic name against pre-extracted heading titles, so a
subtopic with no heading of its own, or one phrased differently than the
book's own heading, resolves to nothing and silently falls back to the
whole chapter. This calls real embeddings instead, so it finds the right
passage by meaning, not by exact title string.

Best-effort only: any failure here (network, timeout, service down, book
not yet indexed on that side) returns None so the caller falls back to the
existing whole-chapter grounding — a slice of the book beats a broken
request, same "fails closed" philosophy as the rest of this module. The
service itself has no retry of its own yet, so this client doesn't lean on
it recovering anything — one attempt, fail quiet, fall back."""
import os
from typing import Optional

import httpx

RAG_URL = os.environ.get("TEXTBOOK_RAG_URL", "https://text-book-rag.onrender.com").rstrip("/")
RAG_TIMEOUT_S = 20.0


def retrieve_textbook_content(
    book_id: str, chapter: Optional[int], query: str, top_k_text: int = 5, top_k_images: int = 3,
) -> Optional[dict]:
    try:
        with httpx.Client(timeout=RAG_TIMEOUT_S) as client:
            res = client.post(
                f"{RAG_URL}/retrieve-content",
                json={
                    "book_id": book_id, "chapter": chapter, "query": query,
                    "top_k_text": top_k_text, "top_k_images": top_k_images,
                },
            )
            res.raise_for_status()
            return res.json()
    except Exception as e:
        print(f"[textbook_rag_client] retrieve failed for {book_id}/chapter {chapter}: {e}")
        return None
