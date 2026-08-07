-- Job queue schema.
--
-- Two tables rather than one status column, so queue_jobs stays the size of the backlog
-- rather than the size of everything that has ever run.

CREATE TABLE queue_jobs (
    id             bigserial PRIMARY KEY,
    queue          varchar(64) NOT NULL DEFAULT 'default',
    name           varchar(190) NOT NULL,

    -- JSON rather than pickle: payloads survive a deploy that changes the class, they can
    -- be read in a SELECT while somebody is asking why a job did not run, and they cannot
    -- become a deserialisation gadget. That last point is the decisive one - an unpickle on
    -- this column would be remote code execution sitting in a table, reachable by anything
    -- that can write a row.
    payload        text NOT NULL,

    attempts       integer NOT NULL DEFAULT 0,
    max_attempts   integer NOT NULL DEFAULT 3,
    priority       integer NOT NULL DEFAULT 0,
    unique_key     varchar(190),

    available_at   timestamptz NOT NULL,

    -- A deadline, not a flag. NULL means nobody holds it, a future value means a worker
    -- does, and a past value means a worker did and died - so it is claimable again with
    -- its attempt already spent. That is what makes a killed worker self-healing without a
    -- heartbeat, a lease renewal or an unlock path.
    reserved_until timestamptz,
    reserved_by    varchar(64),

    last_error     text NOT NULL DEFAULT '',
    created_at     timestamptz NOT NULL
);

-- The reserve query's index. reserved_until is deliberately left out: it discards very few
-- rows, and a nullable column here would only stop the index serving the sort.
CREATE INDEX idx_queue_jobs_reserve ON queue_jobs (queue, priority DESC, available_at, id);

-- Repeated NULLs are allowed in a unique index, which is what lets one index cover both
-- "at most one pending job per key" and "most jobs have no key at all".
CREATE UNIQUE INDEX idx_queue_jobs_unique_key ON queue_jobs (unique_key);

CREATE TABLE queue_failed_jobs (
    id        bigserial PRIMARY KEY,
    queue     varchar(64) NOT NULL DEFAULT '',
    name      varchar(190) NOT NULL,
    payload   text NOT NULL,
    attempts  integer NOT NULL DEFAULT 0,
    error     text NOT NULL DEFAULT '',
    failed_at timestamptz NOT NULL
);

CREATE INDEX idx_queue_failed_jobs_failed_at ON queue_failed_jobs (failed_at DESC);

-- Retention on the failed table is the application's decision. `queue forget --before` is
-- the batched delete; nothing prunes it automatically, because a failed job nobody looked
-- at is exactly the thing that should still be there tomorrow.
