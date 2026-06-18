from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.indexes import PartialIndex
from tortoise.migrations.constraints import UniqueConstraint

class Migration(migrations.Migration):
    dependencies = [('models', '0001_initial')]

    initial = False

    operations = [
        ops.AddIndex(
            model_name='Event',
            index=PartialIndex(fields=['status'], name='idx_events_published', condition={'status': 'published'}),
        ),
        ops.AddIndex(
            model_name='NotificationJob',
            index=PartialIndex(fields=['id'], name='idx_jobs_pending', condition={'status': 'pending'}),
        ),
        ops.AddIndex(
            model_name='NotificationLog',
            index=PartialIndex(fields=['idempotency_key'], name='idx_log_processing', condition={'status': 'processing'}),
        ),
        ops.AddIndex(
            model_name='Registration',
            index=PartialIndex(fields=['event_id', 'registered_at'], name='idx_registrations_waitlist_fifo', condition={'status': 'waitlisted'}),
        ),
        ops.AddConstraint(
            model_name='Registration',
            constraint=UniqueConstraint(fields=('event_id', 'student_id'), name='uq_one_active_registration_per_student', condition="status IN ('confirmed', 'waitlisted')"),
        ),
    ]
