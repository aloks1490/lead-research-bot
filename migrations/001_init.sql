-- Lead-research bot schema. Run once in the Supabase SQL editor (or: psql "$DATABASE_URL" -f migrations/001_init.sql).
-- Every table has a unique key that does the deduplication, and every write in the code is an upsert on that key,
-- so a retried step overwrites a row instead of adding a second one.

create extension if not exists pgcrypto;  -- gen_random_uuid()

-- One row per company, shared by every prospect who works there.
create table if not exists companies (
    domain          text primary key,
    name            text,
    industry        text,
    employee_count  integer,
    country         text,
    description     text,
    raw             jsonb not null default '{}'::jsonb,  -- the full Clay payload, to backfill new fields for free
    updated_at      timestamptz not null default now()
);

-- The cache. Checked before every Clay call.
create table if not exists prospects (
    id                 uuid primary key default gen_random_uuid(),
    linkedin_url_norm  text not null unique,
    name               text,
    title              text,
    email              text,
    location           text,
    role_started_on    date,
    company_domain     text references companies (domain),
    enriched_at        timestamptz not null,
    source             text not null default 'clay',
    raw                jsonb not null default '{}'::jsonb
);

-- One row per Slack request. slack_event_id makes Slack's resends harmless; status is the state machine.
create table if not exists research_jobs (
    id                 uuid primary key default gen_random_uuid(),
    slack_event_id     text not null unique,
    status             text not null default 'received' check (status in (
                           'received', 'normalised', 'cache_hit', 'cache_miss', 'enriching', 'enriched',
                           'checked', 'briefed', 'done', 'failed', 'rejected')),
    raw_text           text not null,
    linkedin_url_norm  text,
    requested_by       text,
    channel            text not null,
    thread_ts          text not null,
    attempts           integer not null default 0,
    last_error         text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

-- The sweeper looks for unfinished jobs by status and age.
create index if not exists research_jobs_open_idx on research_jobs (status, updated_at)
    where status not in ('done', 'failed', 'rejected');
create index if not exists research_jobs_url_idx on research_jobs (linkedin_url_norm) where status = 'enriching';

-- Audit trail: what Claude was given, what it wrote, whether the checker passed it, and what was delivered.
create table if not exists briefs (
    job_id            uuid primary key references research_jobs (id),
    prospect_id       uuid references prospects (id),
    suppression       jsonb,          -- HubSpot state and the verdict computed in code
    facts             jsonb,          -- the exact fact sheet sent to the model
    brief             jsonb,
    source            text,           -- 'claude' or 'template'
    model             text,
    check_passed      boolean,
    fallback_reason   text,
    input_tokens      integer,
    output_tokens     integer,
    cache_read_tokens integer,
    slack_ts          text,           -- set once the brief is posted, so a resumed job does not post twice
    hubspot_note_id   text,           -- set once the note is written, for the same reason
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now()
);

-- Supabase exposes public tables through its REST API. Turning on row-level security with no policies closes
-- that door; this service connects as the table owner over Postgres and is not affected.
alter table companies enable row level security;
alter table prospects enable row level security;
alter table research_jobs enable row level security;
alter table briefs enable row level security;
