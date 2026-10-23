-- A message can now carry a structured feedback request — {"classId",
-- "className", "subject", "topic", "subtopic"} — rendered in the frontend as
-- a real three-question (Yes/Somewhat/No) + free-text card, same shape
-- Classroom Mode's own post-lesson reflection screen already uses, instead
-- of relying on the teacher typing a free-text reply for the LLM to
-- interpret. feedback_submitted_at mirrors confirmed_at's job: marks the
-- card answered so it stops rendering as interactive. Both nullable and
-- additive — every existing row/caller is unaffected.
alter table group_chat_messages add column if not exists feedback_request jsonb;
alter table group_chat_messages add column if not exists feedback_submitted_at timestamptz;
