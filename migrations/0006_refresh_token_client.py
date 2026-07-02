from tortoise import migrations
from tortoise.migrations import operations as ops


class Migration(migrations.Migration):
    """Bind each refresh token to the client it was issued to (HEF-44).

    Delivery is fixed at issuance: ``web`` tokens are only ever returned via the
    httpOnly ``hefest_refresh`` cookie, ``mobile`` tokens only in the response
    body. Existing rows were all minted through the cookie flow.

    Written as raw SQL (not ``ops.AddField``) because the column is ``NOT NULL``
    and the table may already hold rows: the ORM-level ``default`` does not emit
    a SQL ``DEFAULT``, so a plain AddField would fail to backfill. The default is
    added to populate existing rows, then dropped so future inserts rely on the
    ORM (which always supplies ``client``), matching the model definition.
    """

    dependencies = [("models", "0005_waitlist_fifo_index_add_id")]

    initial = False

    operations = [
        ops.RunSQL(
            sql="""
ALTER TABLE refresh_tokens ADD COLUMN client VARCHAR(8) NOT NULL DEFAULT 'web';
ALTER TABLE refresh_tokens ALTER COLUMN client DROP DEFAULT;
""",
            reverse_sql="ALTER TABLE refresh_tokens DROP COLUMN client;",
        ),
    ]
