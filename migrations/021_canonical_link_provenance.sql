-- Records the raw, as-extracted name on each topic->canonical link, distinct
-- from whatever the entry is canonically named today. Without this, once
-- "Recognize Numerals" gets folded into "Number Recognition" as an alias, the
-- system has no memory of which specific topic links actually came from that
-- phrasing vs. from "Number Recognition" directly — which means a bad merge
-- can never be selectively undone, only manually fixed. This is what makes
-- app/lib/canonical_mapping.py's split_canonical() possible.

alter table topic_concepts add column if not exists source_name text;
alter table topic_competencies add column if not exists source_name text;
alter table topic_vocabulary add column if not exists source_name text;
alter table topic_contexts add column if not exists source_name text;
