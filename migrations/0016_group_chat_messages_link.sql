-- A message can now carry a real clickable link — {"label": "...", "href":
-- "..."} — same structured-jsonb style trace/pending_confirmation already
-- use on this table. First use: the pre-class "prep material is ready"
-- briefing (teacher_period_briefing.py) linking straight to the real
-- material instead of just saying "open Prep Materials" in plain text.
-- Nullable and additive — every existing row/caller is unaffected.
alter table group_chat_messages add column if not exists link jsonb;
