from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """Allow account-scoped notification jobs with no event (HEF-39 follow-up).

    Email verification is delivered through the same Postgres-outbox worker as
    registration emails, but it is account-scoped and has no ``Event``. The
    ``event_id`` foreign key therefore becomes nullable so an ``EmailVerify``
    job can be enqueued with ``event_id = NULL``. The FK and its ``ON DELETE
    CASCADE`` are unchanged for event-scoped rows; NULL simply bypasses it.
    """

    dependencies = [("models", "0006_notification_jobs_outbox_delivery")]

    initial = False

    operations = [
        ops.RunSQL(
            sql="ALTER TABLE notification_jobs ALTER COLUMN event_id DROP NOT NULL;",
            reverse_sql=(
                "DELETE FROM notification_jobs WHERE event_id IS NULL;\n"
                "ALTER TABLE notification_jobs ALTER COLUMN event_id SET NOT NULL;"
            ),
        ),
    ]
