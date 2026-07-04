from uuid import uuid4

from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.migrations import operations as ops

from hefest.models.notification import NotificationType


class Migration(migrations.Migration):
    """Create the notifications table for the in-app notification feed.

    Additive-only: no existing table is touched, so ``0001``-``0008`` are
    unaffected. The composite index ``(user_id, read_at, created_at DESC)`` is
    created via ``RunSQL`` because the ``DESC`` ordering cannot be expressed in
    the model ``Meta``; it covers every query the router runs (unread count,
    unread list, paginated feed), which all filter on ``user_id`` and order by
    ``created_at`` descending.
    """

    dependencies = [("models", "0008_notification_jobs_nullable_event")]

    initial = False

    operations = [
        ops.CreateModel(
            name="Notification",
            fields=[
                ("id", fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ("user", fields.ForeignKeyField("models.User", source_field="user_id", db_constraint=True, to_field="id", related_name="notifications", on_delete=OnDelete.CASCADE)),
                ("event", fields.ForeignKeyField("models.Event", source_field="event_id", db_constraint=True, to_field="id", related_name="notifications", on_delete=OnDelete.CASCADE, null=True)),
                ("notification_type", fields.CharEnumField(NotificationType, max_length=32)),
                ("payload", fields.JSONField()),
                ("read_at", fields.DatetimeField(auto_now=False, auto_now_add=False, null=True)),
                ("created_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={"table": "notifications", "app": "models", "pk_attr": "id", "table_description": "A personal, per-user notification shown in the in-app dropdown feed."},
            bases=["Model"],
        ),
        ops.RunSQL(
            sql=(
                "CREATE INDEX idx_notifications_user_unread_created\n"
                "    ON notifications (user_id, read_at, created_at DESC);"
            ),
            reverse_sql="DROP INDEX IF EXISTS idx_notifications_user_unread_created;",
        ),
    ]
