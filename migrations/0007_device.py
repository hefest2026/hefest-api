from uuid import uuid4

from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.migrations import operations as ops

from hefest.models.device import DevicePlatform


class Migration(migrations.Migration):
    """Create the devices table for Expo push-token registration (HEF-45)."""

    dependencies = [("models", "0006_refresh_token_client")]

    initial = False

    operations = [
        ops.CreateModel(
            name="Device",
            fields=[
                ("id", fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ("user", fields.ForeignKeyField("models.User", source_field="user_id", db_constraint=True, to_field="id", related_name="devices", on_delete=OnDelete.CASCADE)),
                ("expo_push_token", fields.CharField(unique=True, max_length=255)),
                ("platform", fields.CharEnumField(DevicePlatform, max_length=8)),
                ("created_at", fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ("updated_at", fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={"table": "devices", "app": "models", "pk_attr": "id", "table_description": "A device that receives push notifications for a user (Expo push token)."},
            bases=["Model"],
        ),
    ]
