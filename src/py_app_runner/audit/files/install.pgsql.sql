-- Audit trail schema.
--
-- One row per recorded change. `module` is a column rather than a table name, so
-- "everything this user did today" stays one query and request_id can group a change that
-- crossed several modules.

CREATE TABLE audit_log (
    id          bigserial PRIMARY KEY,

    -- Written by the application in UTC rather than left to this default, so the value does
    -- not depend on the database server's clock or timezone. The default only covers rows
    -- inserted by something else.
    created_at  timestamptz NOT NULL DEFAULT now(),

    request_id  varchar(32) NOT NULL DEFAULT '',
    module      varchar(64) NOT NULL DEFAULT '',
    event       varchar(32) NOT NULL,

    -- The table name and its key as text, because one application keys on bigint and the
    -- next on uuid. No foreign key: an audit row has to outlive what it describes.
    entity_type varchar(128) NOT NULL,
    entity_id   varchar(64) NOT NULL DEFAULT '',

    actor_type  varchar(32) NOT NULL DEFAULT '',
    actor_id    varchar(64) NOT NULL DEFAULT '',

    -- Denormalised on purpose. Joining a users table later means deleting or renaming a
    -- user rewrites what the log says they did.
    actor_name  varchar(190) NOT NULL DEFAULT '',

    -- Split rather than one {from,to} blob, so a value can be queried directly:
    -- WHERE new_values ->> 'status' = 'cancelled'
    old_values  jsonb,
    new_values  jsonb,

    url         text NOT NULL DEFAULT '',
    ip_address  varchar(45) NOT NULL DEFAULT '',
    user_agent  text NOT NULL DEFAULT '',
    tags        jsonb,
    context     jsonb
);

-- The history of one record, which is what the trail is opened for.
CREATE INDEX idx_audit_log_entity ON audit_log (entity_type, entity_id, created_at DESC);

-- The default sort of any viewer, and what the prune command scans.
CREATE INDEX idx_audit_log_created ON audit_log (created_at DESC);

-- What one person did.
CREATE INDEX idx_audit_log_actor ON audit_log (actor_id, created_at DESC);

-- Deliberately not created here, because each one is write amplification on what becomes
-- the busiest table in the database. Add them when a query needs them:
--
--   CREATE INDEX idx_audit_log_request ON audit_log (request_id);
--   CREATE INDEX idx_audit_log_module ON audit_log (module, created_at DESC);
--   CREATE INDEX idx_audit_log_new_values ON audit_log USING gin (new_values);
--
-- The gin index makes searching inside a change possible and roughly doubles insert cost.

-- Nothing edits an audit row. Granting the application only what it needs is what makes
-- that true rather than merely intended:
--
--   REVOKE UPDATE, DELETE ON audit_log FROM <application role>;
--
-- with the retention job running as a role that does keep DELETE.

-- Retention is the application's decision, not the framework's. `audit prune` deletes in
-- batches. On a large installation, declare the table partitioned by month instead and
-- drop whole partitions:
--
--   CREATE TABLE audit_log (...) PARTITION BY RANGE (created_at);
--
-- which requires the partition key in the primary key, so it becomes
-- PRIMARY KEY (id, created_at).
