from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """notification_jobs becomes the single source of truth for delivery (HEF-39).

    The Redis-Streams relay is replaced by a Postgres-outbox worker, so the
    ``notification_jobs`` row now carries delivery state directly and the
    separate ``notification_log`` table is removed.

    Operations are ordered deliberately:

    1. Add the delivery columns. ``next_attempt_at`` carries
       ``DEFAULT statement_timestamp()`` so PostgreSQL backfills existing rows at
       ALTER TABLE time (it is ``NOT NULL``); ``attempts`` defaults to 0. The
       defaults are also the backstop for non-ORM inserts.
    2. Data fix: rewrite the legacy ``published`` status to ``pending`` BEFORE
       creating the partial index whose predicate only knows the new states.
    3. Replace the old ``idx_jobs_pending`` index with the claimable/reaper
       partial indexes the worker scans.
    4. Drop the now-unused ``notification_log`` table.

    The status column is a plain varchar (``CharEnumField``), so there is no
    Postgres ENUM type to alter — only the row data is migrated.
    """

    dependencies = [("models", "0005_waitlist_fifo_index_add_id")]

    initial = False

    operations = [
        ops.RunSQL(
            sql="""
ALTER TABLE notification_jobs
    ADD COLUMN attempts INT NOT NULL DEFAULT 0,
    ADD COLUMN locked_by TEXT,
    ADD COLUMN heartbeat_at TIMESTAMPTZ,
    ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    ADD COLUMN last_error TEXT;

UPDATE notification_jobs SET status = 'pending' WHERE status = 'published';

DROP INDEX IF EXISTS idx_jobs_pending;

CREATE INDEX idx_jobs_claimable ON notification_jobs (next_attempt_at, id)
    WHERE status IN ('pending', 'processing');

CREATE INDEX idx_jobs_reaper ON notification_jobs (heartbeat_at)
    WHERE status = 'processing';

DROP TABLE notification_log;
""",
            reverse_sql="""
CREATE TABLE notification_log (
    id UUID NOT NULL PRIMARY KEY,
    idempotency_key VARCHAR(512) NOT NULL UNIQUE,
    status VARCHAR(16) NOT NULL,
    attempts INT NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp()
);
CREATE INDEX idx_log_processing ON notification_log (idempotency_key)
    WHERE status = 'processing';

DROP INDEX IF EXISTS idx_jobs_reaper;
DROP INDEX IF EXISTS idx_jobs_claimable;

CREATE INDEX idx_jobs_pending ON notification_jobs (id)
    WHERE status = 'pending';

ALTER TABLE notification_jobs
    DROP COLUMN last_error,
    DROP COLUMN next_attempt_at,
    DROP COLUMN heartbeat_at,
    DROP COLUMN locked_by,
    DROP COLUMN attempts;
""",
        ),
    ]
