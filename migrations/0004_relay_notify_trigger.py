from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """LISTEN/NOTIFY wake signal for the outbox relay (HEF-16).

    A statement-level AFTER INSERT trigger on ``notification_jobs`` emits
    ``pg_notify`` so the relay drains within milliseconds of the API's COMMIT
    instead of waiting for its fallback poll. NOTIFY is transactional — the
    signal fires at COMMIT, exactly when the outbox row becomes visible — so
    there is no dual-write race. The payload is intentionally empty: the relay
    re-queries the table, keeping this independent of payload size limits and PII.

    Statement-level (not row-level) keeps a single bulk insert — e.g. the
    EventCancelled fan-out — to one wake signal instead of one per row.

    The channel name MUST match ``settings.relay_notify_channel`` ("hefest_jobs").
    """

    dependencies = [("models", "0003_auto_20260619_1539")]

    operations = [
        ops.RunSQL(
            sql="""
CREATE OR REPLACE FUNCTION hefest_notify_pending_job() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('hefest_jobs', '');
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_notify_pending_job ON notification_jobs;

CREATE TRIGGER trg_notify_pending_job
    AFTER INSERT ON notification_jobs
    FOR EACH STATEMENT
    EXECUTE FUNCTION hefest_notify_pending_job();
""",
            reverse_sql="""
DROP TRIGGER IF EXISTS trg_notify_pending_job ON notification_jobs;
DROP FUNCTION IF EXISTS hefest_notify_pending_job();
""",
        ),
    ]
