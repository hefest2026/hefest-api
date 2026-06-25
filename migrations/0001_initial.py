import functools
from json import dumps, loads
from uuid import uuid4

from tortoise import fields, migrations
from tortoise.fields.base import OnDelete
from tortoise.indexes import Index
from tortoise.migrations import operations as ops

from hefest.models.event import EventStatus
from hefest.models.notification_job import JobStatus
from hefest.models.notification_log import DeliveryStatus
from hefest.models.registration import RegistrationStatus
from hefest.models.user import UserRole


class Migration(migrations.Migration):
    initial = True

    operations = [
        ops.CreateModel(
            name='NotificationLog',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('idempotency_key', fields.CharField(unique=True, max_length=512)),
                ('status', fields.CharEnumField(description='processing: processing\ncompleted: completed\nfailed: failed', enum_type=DeliveryStatus, max_length=16)),
                ('attempts', fields.IntField(default=1)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'notification_log', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Delivery log written by the C++ worker.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='User',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('email', fields.CharField(unique=True, max_length=254)),
                ('password_hash', fields.TextField(unique=False)),
                ('full_name', fields.CharField(max_length=255)),
                ('role', fields.CharEnumField(description='student: student\norganizer: organizer', enum_type=UserRole, max_length=16)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'users', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Application user — either a student or an organizer.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Event',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('organizer', fields.ForeignKeyField('models.User', source_field='organizer_id', db_constraint=True, to_field='id', related_name='events', on_delete=OnDelete.CASCADE)),
                ('title', fields.TextField(unique=False)),
                ('description', fields.TextField(default='', unique=False)),
                ('starts_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('ends_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('location', fields.TextField(unique=False)),
                ('capacity', fields.IntField()),
                ('status', fields.CharEnumField(default=EventStatus.draft, description='draft: draft\npublished: published\ncancelled: cancelled', enum_type=EventStatus, max_length=16)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'events', 'app': 'models', 'indexes': [Index(fields=['organizer_id'])], 'pk_attr': 'id', 'table_description': 'A school event created by an organizer.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='NotificationJob',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('event', fields.ForeignKeyField('models.Event', source_field='event_id', db_constraint=True, to_field='id', related_name='notification_jobs', on_delete=OnDelete.CASCADE)),
                ('event_type', fields.TextField(unique=False)),
                ('payload', fields.JSONField(encoder=functools.partial(dumps, separators=(',', ':')), decoder=loads)),
                ('status', fields.CharEnumField(default=JobStatus.pending, description='pending: pending\npublished: published', enum_type=JobStatus, max_length=16)),
                ('idempotency_key', fields.CharField(unique=True, max_length=512)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('updated_at', fields.DatetimeField(auto_now=True, auto_now_add=False)),
            ],
            options={'table': 'notification_jobs', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Transactional outbox row — bridges DB writes to Redis Streams.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='Registration',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('event', fields.ForeignKeyField('models.Event', source_field='event_id', db_constraint=True, to_field='id', related_name='registrations', on_delete=OnDelete.CASCADE)),
                ('student', fields.ForeignKeyField('models.User', source_field='student_id', db_constraint=True, to_field='id', related_name='registrations', on_delete=OnDelete.CASCADE)),
                ('status', fields.CharEnumField(description='confirmed: confirmed\nwaitlisted: waitlisted\ncancelled: cancelled', enum_type=RegistrationStatus, max_length=16)),
                ('registered_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
                ('cancelled_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
            ],
            options={'table': 'registrations', 'app': 'models', 'indexes': [Index(fields=['event_id', 'status']), Index(fields=['student_id'])], 'pk_attr': 'id', 'table_description': "A student's registration for an event."},
            bases=['Model'],
        ),
    ]
