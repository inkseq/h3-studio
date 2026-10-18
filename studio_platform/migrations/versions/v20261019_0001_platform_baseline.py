"""Frozen 59-table platform baseline at source 35b3b20.

Generated once from all 11 registered metadata families, then reviewed. Do not
import live models here: historical DDL must not change with application code.
No existing database is stamped or upgraded by this revision's introduction.
"""

from alembic import op
import sqlalchemy as sa

revision = "20261019_0001"
down_revision = None
branch_labels = None
depends_on = None


def build(operations):
    operations.create_table(
        'platform_accounts',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('disabled', sa.Integer(), nullable=False),
        sa.Column('updated', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'username'),
    )
    operations.create_table(
        'platform_agent_connection_audit',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('connection_id', sa.String(length=80), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('event', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'id'),
    )
    operations.create_table(
        'platform_agent_connections',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('session_hash', sa.String(length=64), nullable=False),
        sa.Column('auth_mode', sa.String(length=20), nullable=False),
        sa.Column('password_version', sa.Float(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('claimed_at', sa.Float(), nullable=True),
        sa.Column('recovery_until', sa.Float(), nullable=True),
        sa.Column('revoked_at', sa.Float(), nullable=True),
        sa.Column('client_challenge', sa.String(length=64), nullable=True),
        sa.Column('key_id', sa.String(length=80), nullable=True),
        sa.Column('token_hash', sa.String(length=64), nullable=True),
        sa.Column('authorization', sa.Text(), nullable=False),
        sa.Column('authorization_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('idempotency_key', sa.String(length=160), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'id'),
        sa.UniqueConstraint('code_hash'),
        sa.UniqueConstraint(
            'tenant', 'owner', 'idempotency_key', name='uq_agent_connect_issue'
        ),
    )
    operations.create_table(
        'platform_agent_exchange_limits',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('source_hash', sa.String(length=64), nullable=False),
        sa.Column('window_start', sa.Float(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'source_hash'),
    )
    operations.create_table(
        'platform_artifact_storage_accounting',
        sa.Column('scope', sa.String(length=320), nullable=False),
        sa.Column('object_id', sa.String(length=64), nullable=False),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint('scope', 'object_id'),
    )
    operations.create_table(
        'platform_artifact_write_receipts',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant', sa.String(length=80), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('attempt_id', sa.String(length=36), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('record', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_asset_storage_quota',
        sa.Column('scope', sa.String(length=320), nullable=False),
        sa.Column('bytes', sa.BigInteger(), nullable=False),
        sa.Column('active', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('scope'),
    )
    operations.create_table(
        'platform_asset_upload_receipts',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant', sa.String(length=80), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('project_id', sa.String(length=160), nullable=False),
        sa.Column('client_key', sa.String(length=128), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('record', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant', 'owner', 'project_id', 'client_key', name='uq_asset_upload_client'
        ),
    )
    operations.create_table(
        'platform_assets',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant', sa.String(length=80), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('project_id', sa.String(length=160), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('created', sa.Float(), nullable=False),
        sa.Column('record', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_batches',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('owner', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('items', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('cancel_requested', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant', 'owner', 'project_id', 'actor_id', 'idempotency_key'
        ),
    )
    operations.create_table(
        'platform_billing_adjustments',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('reference_type', sa.String(length=30), nullable=False),
        sa.Column('reference_id', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=30), nullable=False),
        sa.Column('action_id', sa.String(length=36), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('reason', sa.String(length=1000), nullable=False),
        sa.Column('evidence_sha256', sa.String(length=64), nullable=False),
        sa.Column('evidence_note', sa.String(length=2000), nullable=False),
        sa.Column('basis_exposure_microusd', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'owner_id', 'reference_type', 'reference_id', 'kind'
        ),
    )
    operations.create_table(
        'platform_budget_accounts',
        sa.Column('id', sa.String(length=200), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=True),
        sa.Column('project_id', sa.String(length=200), nullable=True),
        sa.Column('limit_microusd', sa.BigInteger(), nullable=False),
        sa.Column('reserved_microusd', sa.BigInteger(), nullable=False),
        sa.Column('spent_microusd', sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            'reserved_microusd >= 0 AND spent_microusd >= 0 AND limit_microusd >= 0'
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_capacity_approvals',
        sa.Column('id', sa.String(length=200), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('configuration_id', sa.String(length=200), nullable=False),
        sa.Column('approval_hash', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_capacity_gate',
        sa.Column('id', sa.String(length=30), nullable=False),
        sa.Column('max_instances', sa.Integer(), nullable=False),
        sa.Column('max_physical_gpus', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_capacity_market_observations',
        sa.Column('provider', sa.String(length=30), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('provider'),
    )
    operations.create_table(
        'platform_documents',
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('kind', sa.String(length=200), nullable=False),
        sa.Column('document_id', sa.String(length=200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint(
            'tenant_id', 'owner_id', 'project_id', 'kind', 'document_id'
        ),
    )
    operations.create_table(
        'platform_edit_receipts',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('owner', sa.String(length=200), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('key', sa.String(length=160), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('response', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'owner', 'actor', 'project_id', 'key'),
    )
    operations.create_table(
        'platform_instance_intents',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('intent_key', sa.String(length=200), nullable=False),
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('state', sa.String(length=40), nullable=False),
        sa.Column('physical_gpus', sa.Integer(), nullable=False),
        sa.Column('slots', sa.Integer(), nullable=False),
        sa.Column('reserved_cost_microusd', sa.BigInteger(), nullable=False),
        sa.Column('provider_instance_id', sa.String(length=200), nullable=True),
        sa.Column('hard_deadline', sa.Float(), nullable=True),
        sa.Column(
            'provider', sa.String(length=200), server_default='unknown', nullable=False
        ),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('pool', 'intent_key'),
    )
    operations.create_table(
        'platform_login_limits',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('source_hash', sa.String(length=64), nullable=False),
        sa.Column('window_start', sa.Float(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'source_hash'),
    )
    operations.create_table(
        'platform_operator_capacity_commands',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('reason_code', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('actor', 'idempotency_key'),
    )
    operations.create_table(
        'platform_operator_capacity_heartbeats',
        sa.Column('id', sa.String(length=30), nullable=False),
        sa.Column('controller_id', sa.String(length=200), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_operator_capacity_inventory',
        sa.Column('binding_id', sa.String(length=200), nullable=False),
        sa.Column('binding_hash', sa.String(length=64), nullable=False),
        sa.Column('controller_id', sa.String(length=200), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('status', sa.String(length=30), nullable=False),
        sa.Column('reason_code', sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint('binding_id'),
    )
    operations.create_table(
        'platform_operator_capacity_policy',
        sa.Column('id', sa.String(length=30), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_operator_capacity_previews',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('actor', sa.String(length=200), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_operator_machine_groups',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_outbox',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('event_type', sa.String(length=100), nullable=False),
        sa.Column('aggregate_id', sa.String(length=200), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('delivered_at', sa.Float(), nullable=True),
        sa.Column('job_event_seq', sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_index(
        'platform_outbox_job_event_order',
        'platform_outbox',
        ['job_event_seq'],
        unique=True,
    )
    operations.create_table(
        'platform_owner_usage',
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('owner_key', sa.String(length=500), nullable=False),
        sa.Column('work_s', sa.Float(), nullable=False),
        sa.UniqueConstraint('pool', 'owner_key'),
    )
    operations.create_table(
        'platform_personal_api_keys',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('prefix', sa.String(length=20), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('projects', sa.Text(), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('all_projects', sa.Integer(), nullable=False),
        sa.Column('auth_mode', sa.String(length=20), nullable=False),
        sa.Column('password_version', sa.Float(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('last_used_at', sa.Float(), nullable=True),
        sa.Column('revoked_at', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('tenant', 'id'),
        sa.UniqueConstraint('token_hash'),
    )
    operations.create_table(
        'platform_plans',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('execution_plan', sa.JSON(), nullable=False),
        sa.Column('plan_hash', sa.String(length=64), nullable=False),
        sa.Column('estimated_cost_microusd', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_pool_limits',
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('max_instances', sa.Integer(), nullable=False),
        sa.Column('max_physical_gpus', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('pool'),
    )
    operations.create_table(
        'platform_project_activity',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('project_version', sa.Integer(), nullable=False),
        sa.Column('actor_kind', sa.String(length=20), nullable=False),
        sa.Column('actor_label', sa.String(length=80), nullable=False),
        sa.Column('event_type', sa.String(length=40), nullable=False),
        sa.Column('summary', sa.String(length=300), nullable=False),
        sa.Column('operations', sa.JSON(), nullable=False),
        sa.Column('target_entity_ids', sa.JSON(), nullable=False),
        sa.Column('target_count', sa.Integer(), nullable=False),
        sa.Column('occurred_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant_id', 'owner_id', 'project_id', 'project_version'),
    )
    operations.create_table(
        'platform_quick_chat_events',
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('owner', sa.String(length=200), nullable=False),
        sa.Column('session_id', sa.String(length=80), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('type', sa.String(length=60), nullable=False),
        sa.Column('resource_id', sa.String(length=80), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant', 'owner', 'session_id', 'seq'),
    )
    operations.create_table(
        'platform_quick_chat_objects',
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('owner', sa.String(length=200), nullable=False),
        sa.Column('session_id', sa.String(length=80), nullable=False),
        sa.Column('kind', sa.String(length=40), nullable=False),
        sa.Column('parent_id', sa.String(length=80), nullable=True),
        sa.Column('business_key', sa.String(length=200), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('tenant', 'owner', 'kind', 'business_key'),
    )
    operations.create_table(
        'platform_quick_chat_operations',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('owner', sa.String(length=200), nullable=False),
        sa.Column('namespace', sa.String(length=200), nullable=False),
        sa.Column('key', sa.String(length=160), nullable=False),
        sa.Column('fingerprint', sa.String(length=64), nullable=False),
        sa.Column('object_id', sa.String(length=80), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'owner', 'namespace', 'key'),
    )
    operations.create_table(
        'platform_registered_workers',
        sa.Column('id', sa.String(length=200), nullable=False),
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('provider', sa.String(length=200), nullable=False),
        sa.Column('instance_id', sa.String(length=200), nullable=False),
        sa.Column('spec', sa.JSON(), nullable=False),
        sa.Column('spec_hash', sa.String(length=64), nullable=False),
        sa.Column('state', sa.String(length=40), nullable=False),
        sa.Column('current_job_id', sa.String(length=36), nullable=True),
        sa.Column('drain_requested', sa.Integer(), server_default='0', nullable=False),
        sa.Column('fence', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_scaler_leaders',
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('leader_id', sa.String(length=200), nullable=False),
        sa.Column('fence', sa.BigInteger(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('policy_hash', sa.String(length=64), nullable=True),
        sa.Column('consecutive_breaches', sa.Integer(), nullable=False),
        sa.Column('last_scale_at', sa.Float(), nullable=True),
        sa.Column('sequence', sa.BigInteger(), nullable=False),
        sa.Column('last_observed_at', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('pool'),
    )
    operations.create_table(
        'platform_scaler_observations',
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('sequence', sa.BigInteger(), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('recommendation', sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint('pool', 'sequence'),
    )
    operations.create_table(
        'platform_scheduler_state',
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('last_owner', sa.String(length=500), nullable=True),
        sa.PrimaryKeyConstraint('pool'),
    )
    operations.create_table(
        'platform_service_clients',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('owner', sa.String(length=80), nullable=False),
        sa.Column('projects', sa.Text(), nullable=False),
        sa.Column('scopes', sa.Text(), nullable=False),
        sa.Column('disabled', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'id'),
        sa.UniqueConstraint('token_hash'),
    )
    operations.create_table(
        'platform_sessions',
        sa.Column('tenant', sa.String(length=200), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('username', sa.String(length=80), nullable=False),
        sa.Column('auth_mode', sa.String(length=20), nullable=False),
        sa.Column('created', sa.Float(), nullable=False),
        sa.Column('expires', sa.Float(), nullable=False),
        sa.Column('password_version', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'token_hash'),
    )
    operations.create_table(
        'platform_spending_confirmations',
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('plan_id', sa.String(length=36), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('plan_hash', sa.String(length=64), nullable=False),
        sa.Column('settings_version', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('consumed_reference', sa.String(length=200), nullable=True),
        sa.PrimaryKeyConstraint('tenant_id', 'owner_id', 'plan_id', 'actor_id'),
    )
    operations.create_table(
        'platform_spending_origins',
        sa.Column('reference_type', sa.String(length=30), nullable=False),
        sa.Column('reference_id', sa.String(length=200), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('reference_type', 'reference_id'),
    )
    operations.create_table(
        'platform_spending_settings',
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('value', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint('tenant_id', 'owner_id'),
    )
    operations.create_table(
        'storage_multipart_fixed_keys',
        sa.Column('identity', sa.String(length=64), nullable=False),
        sa.Column('upload_id', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('identity'),
    )
    operations.create_table(
        'storage_multipart_uploads',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('tenant', sa.String(length=128), nullable=False),
        sa.Column('owner', sa.String(length=128), nullable=False),
        sa.Column('request_key', sa.String(length=128), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('record', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant', 'owner', 'request_key', name='uq_storage_upload_request'
        ),
    )
    operations.create_table(
        'platform_budget_reservations',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('account_id', sa.String(length=200), nullable=False),
        sa.Column('reference_type', sa.String(length=30), nullable=False),
        sa.Column('reference_id', sa.String(length=200), nullable=False),
        sa.Column('amount_microusd', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('actual_cost_microusd', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('settled_at', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ['account_id'],
            ['platform_budget_accounts.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id', 'reference_type', 'reference_id'),
    )
    operations.create_table(
        'platform_capacity_cycles',
        sa.Column('approval_id', sa.String(length=200), nullable=False),
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['platform_capacity_approvals.id'],
        ),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('approval_id'),
        sa.UniqueConstraint('intent_id'),
    )
    operations.create_table(
        'platform_capacity_member_generations',
        sa.Column('approval_id', sa.String(length=200), nullable=False),
        sa.Column('member_id', sa.String(length=200), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('approval_hash', sa.String(length=64), nullable=False),
        sa.Column('previous_intent_id', sa.String(length=36), nullable=False),
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.CheckConstraint('generation > 0 AND generation <= 16'),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['platform_capacity_approvals.id'],
        ),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.ForeignKeyConstraint(
            ['previous_intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('approval_id', 'member_id', 'generation'),
        sa.UniqueConstraint('intent_id'),
        sa.UniqueConstraint('previous_intent_id'),
    )
    operations.create_table(
        'platform_capacity_pool_members',
        sa.Column('approval_id', sa.String(length=200), nullable=False),
        sa.Column('member_id', sa.String(length=200), nullable=False),
        sa.Column('approval_hash', sa.String(length=64), nullable=False),
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['platform_capacity_approvals.id'],
        ),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('approval_id', 'member_id'),
        sa.UniqueConstraint('intent_id'),
    )
    operations.create_table(
        'platform_cpu_slots',
        sa.Column('instance_id', sa.String(length=200), nullable=False),
        sa.Column('worker_id', sa.String(length=200), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ['worker_id'],
            ['platform_registered_workers.id'],
        ),
        sa.PrimaryKeyConstraint('instance_id'),
    )
    operations.create_table(
        'platform_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('tenant_id', sa.String(length=200), nullable=False),
        sa.Column('owner_id', sa.String(length=200), nullable=False),
        sa.Column('project_id', sa.String(length=200), nullable=False),
        sa.Column('actor_id', sa.String(length=200), nullable=False),
        sa.Column('idempotency_key', sa.String(length=200), nullable=False),
        sa.Column('request_hash', sa.String(length=64), nullable=False),
        sa.Column('plan_id', sa.String(length=36), nullable=False),
        sa.Column('request', sa.JSON(), nullable=False),
        sa.Column('execution_plan', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('expected_runtime_s', sa.Float(), nullable=False),
        sa.Column('estimated_cost_microusd', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.Column('not_before', sa.Float(), nullable=False),
        sa.Column('attempt_no', sa.Integer(), nullable=False),
        sa.Column('current_attempt_id', sa.String(length=36), nullable=True),
        sa.Column('fence', sa.BigInteger(), nullable=False),
        sa.Column('lease_worker_id', sa.String(length=200), nullable=True),
        sa.Column('lease_expires_at', sa.Float(), nullable=True),
        sa.Column('cancel_from_status', sa.String(length=40), nullable=True),
        sa.Column('error_code', sa.String(length=200), nullable=True),
        sa.Column('cancellation', sa.JSON(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ['plan_id'],
            ['platform_plans.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'tenant_id', 'owner_id', 'project_id', 'actor_id', 'idempotency_key'
        ),
    )
    operations.create_index(
        'platform_jobs_ready',
        'platform_jobs',
        ['pool', 'status', 'not_before', 'created_at'],
        unique=False,
    )
    operations.create_table(
        'platform_operator_capacity_nodes',
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('command_id', sa.String(length=36), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('binding_id', sa.String(length=200), nullable=False),
        sa.Column('binding_hash', sa.String(length=64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('desired_state', sa.String(length=20), nullable=False),
        sa.Column('runtime_state', sa.String(length=80), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ['command_id'],
            ['platform_operator_capacity_commands.id'],
        ),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('intent_id'),
        sa.UniqueConstraint('command_id', 'ordinal'),
    )
    operations.create_table(
        'platform_registered_devices',
        sa.Column('provider', sa.String(length=200), nullable=False),
        sa.Column('instance_id', sa.String(length=200), nullable=False),
        sa.Column('gpu_id', sa.String(length=200), nullable=False),
        sa.Column('worker_id', sa.String(length=200), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.ForeignKeyConstraint(
            ['worker_id'],
            ['platform_registered_workers.id'],
        ),
        sa.UniqueConstraint('provider', 'instance_id', 'gpu_id'),
    )
    operations.create_table(
        'platform_scaler_actions',
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('pool', sa.String(length=200), nullable=False),
        sa.Column('launch_spec', sa.JSON(), nullable=False),
        sa.Column('create_started_at', sa.Float(), nullable=True),
        sa.Column('destroy_started_at', sa.Float(), nullable=True),
        sa.Column('last_observation', sa.JSON(), nullable=True),
        sa.Column('last_observed_at', sa.Float(), nullable=True),
        sa.Column('application_idle_since', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('intent_id'),
    )
    operations.create_table(
        'platform_scaler_receipts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('intent_id', sa.String(length=36), nullable=False),
        sa.Column('operation', sa.String(length=30), nullable=False),
        sa.Column('observed_at', sa.Float(), nullable=False),
        sa.Column('facts', sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    operations.create_table(
        'platform_attempts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=40), nullable=False),
        sa.Column('fence', sa.BigInteger(), nullable=False),
        sa.Column('worker_id', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.Column('submission_started_at', sa.Float(), nullable=True),
        sa.Column('upstream_task_id', sa.String(length=200), nullable=True),
        sa.Column('upstream_stopped', sa.Integer(), nullable=False),
        sa.Column('collection_failures', sa.Integer(), nullable=False),
        sa.Column('error_code', sa.String(length=200), nullable=True),
        sa.Column('actual_cost_microusd', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(
            ['job_id'],
            ['platform_jobs.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'number'),
    )
    operations.create_table(
        'platform_capacity_waiters',
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('approval_id', sa.String(length=200), nullable=False),
        sa.Column('approval_hash', sa.String(length=64), nullable=False),
        sa.Column('deadline', sa.Float(), nullable=False),
        sa.Column('intent_id', sa.String(length=36), nullable=True),
        sa.Column('state', sa.String(length=40), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ['approval_id'],
            ['platform_capacity_approvals.id'],
        ),
        sa.ForeignKeyConstraint(
            ['intent_id'],
            ['platform_instance_intents.id'],
        ),
        sa.ForeignKeyConstraint(
            ['job_id'],
            ['platform_jobs.id'],
        ),
        sa.PrimaryKeyConstraint('job_id'),
    )
    operations.create_index(
        'platform_capacity_waiters_pending',
        'platform_capacity_waiters',
        ['approval_id', 'state', 'created_at'],
        unique=False,
    )
    operations.create_table(
        'platform_dispatch_receipts',
        sa.Column('event_id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('publisher_id', sa.String(length=200), nullable=True),
        sa.Column('lease_expires_at', sa.Float(), nullable=True),
        sa.Column('external_run_id', sa.String(length=200), nullable=True),
        sa.Column('publish_attempts', sa.Integer(), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('not_before', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.Column('error_code', sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(
            ['event_id'],
            ['platform_outbox.id'],
        ),
        sa.ForeignKeyConstraint(
            ['job_id'],
            ['platform_jobs.id'],
        ),
        sa.PrimaryKeyConstraint('event_id'),
        sa.UniqueConstraint('job_id', 'sequence'),
    )
    operations.create_table(
        'platform_artifacts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('attempt_id', sa.String(length=36), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(
            ['attempt_id'],
            ['platform_attempts.id'],
        ),
        sa.ForeignKeyConstraint(
            ['job_id'],
            ['platform_jobs.id'],
        ),
        sa.PrimaryKeyConstraint('id'),
    )


def upgrade():
    # env.py requires verified adoption. This additional guard protects direct
    # revision execution from colliding with an existing populated schema.
    if not op.get_context().as_sql:
        if set(sa.inspect(op.get_bind()).get_table_names()) - {"alembic_version"}:
            raise RuntimeError("baseline_requires_empty_database")
    build(op)


def downgrade():
    raise RuntimeError("baseline_destructive_downgrade_not_supported")
