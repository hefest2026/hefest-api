from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """Extend idx_registrations_waitlist_fifo to cover the id tie-breaker column.

    The FIFO promotion query in cancel_registration sorts by (registered_at, id).
    The previous index only covered (event_id, registered_at), forcing PostgreSQL
    to do a separate sort pass on `id`. Adding `id` makes the sort fully
    index-covered (index-only scan path available).
    """

    dependencies = [("models", "0004_relay_notify_trigger")]

    operations = [
        ops.RunSQL(
            sql="""
DROP INDEX IF EXISTS idx_registrations_waitlist_fifo;
CREATE INDEX idx_registrations_waitlist_fifo
    ON registrations (event_id, registered_at, id)
    WHERE status = 'waitlisted';
""",
            reverse_sql="""
DROP INDEX IF EXISTS idx_registrations_waitlist_fifo;
CREATE INDEX idx_registrations_waitlist_fifo
    ON registrations (event_id, registered_at)
    WHERE status = 'waitlisted';
""",
        ),
    ]
