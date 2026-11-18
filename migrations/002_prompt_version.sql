-- Which prompt skill file wrote each brief, e.g. "brief@2+3f9a1c2d" (name @ version + content hash).
-- Lets you compare briefs before and after a prompt change. Safe to run twice.
alter table briefs add column if not exists prompt_version text;
