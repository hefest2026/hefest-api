from tortoise import migrations
from tortoise.migrations import operations as ops
from tortoise.fields.base import OnDelete
from uuid import uuid4
from tortoise import fields

class Migration(migrations.Migration):
    dependencies = [('models', '0002_auto_20260618_2240')]

    initial = False

    operations = [
        ops.CreateModel(
            name='OAuthIdentity',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('models.User', source_field='user_id', db_constraint=True, to_field='id', related_name='oauth_identities', on_delete=OnDelete.CASCADE)),
                ('provider', fields.CharField(max_length=32)),
                ('subject', fields.CharField(max_length=255)),
                ('email', fields.CharField(max_length=254)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'oauth_identities', 'app': 'models', 'unique_together': (('provider', 'subject'),), 'pk_attr': 'id', 'table_description': 'Links a provider identity (provider + subject) to a Hefest user.'},
            bases=['Model'],
        ),
        ops.CreateModel(
            name='RefreshToken',
            fields=[
                ('id', fields.UUIDField(primary_key=True, default=uuid4, unique=True, db_index=True)),
                ('user', fields.ForeignKeyField('models.User', source_field='user_id', db_constraint=True, to_field='id', related_name='refresh_tokens', on_delete=OnDelete.CASCADE)),
                ('token_hash', fields.CharField(unique=True, max_length=64)),
                ('expires_at', fields.DatetimeField(auto_now=False, auto_now_add=False)),
                ('revoked_at', fields.DatetimeField(null=True, auto_now=False, auto_now_add=False)),
                ('created_at', fields.DatetimeField(auto_now=False, auto_now_add=True)),
            ],
            options={'table': 'refresh_tokens', 'app': 'models', 'pk_attr': 'id', 'table_description': 'Server-side record of an issued refresh token (stores hash only).'},
            bases=['Model'],
        ),
        ops.AlterField(
            model_name='User',
            name='password_hash',
            field=fields.TextField(null=True, unique=False),
        ),
        ops.AddField(
            model_name='User',
            name='email_verified_at',
            field=fields.DatetimeField(null=True, auto_now=False, auto_now_add=False),
        ),
    ]
