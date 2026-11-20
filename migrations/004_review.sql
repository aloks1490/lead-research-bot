-- The reviewer's verdict on each model-written brief: passed / rejected / skipped, the claims it flagged, which
-- model reviewed it and how long it took. Safe to run twice.
alter table briefs add column if not exists review jsonb;
