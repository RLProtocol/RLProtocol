import os
from src.db.client import db_exec


def init_db():
    # Users — one per email
    db_exec("""
    CREATE TABLE IF NOT EXISTS users (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      email TEXT UNIQUE NOT NULL,
      phone_number TEXT,
      tier TEXT NOT NULL DEFAULT 'free' CHECK (tier IN ('free', 'pro', 'team')),
      stripe_customer_id TEXT,
      stripe_subscription_id TEXT,
      minutes_used_this_month INTEGER NOT NULL DEFAULT 0,
      minutes_limit INTEGER NOT NULL DEFAULT 10,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # Agents — one per user on free/pro, up to 5 on team
    db_exec("""
    CREATE TABLE IF NOT EXISTS agents (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      name TEXT NOT NULL DEFAULT 'My Agent',
      webhook_url TEXT NOT NULL,
      api_key_hash TEXT UNIQUE NOT NULL,
      voice TEXT NOT NULL DEFAULT 'Polly.Aria-Neural',
      webhook_push_url TEXT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)
    db_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS voice TEXT NOT NULL DEFAULT 'Polly.Aria-Neural';")
    db_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS voice_provider TEXT NOT NULL DEFAULT 'twilio_polly';")
    db_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS realtime_model TEXT DEFAULT 'gpt-realtime';")
    db_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS realtime_voice TEXT DEFAULT 'marin';")
    db_exec("ALTER TABLE agents ADD COLUMN IF NOT EXISTS webhook_push_url TEXT;")

    # Phone Numbers — dedicated or shared pool
    db_exec("""
    CREATE TABLE IF NOT EXISTS phone_numbers (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      twilio_sid TEXT UNIQUE NOT NULL,
      number TEXT UNIQUE NOT NULL,
      agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
      is_dedicated BOOLEAN NOT NULL DEFAULT FALSE,
      is_shared_pool BOOLEAN NOT NULL DEFAULT FALSE,
      assigned_at TIMESTAMPTZ
    );
    """)

    # Call Logs — every call recorded here
    db_exec("""
    CREATE TABLE IF NOT EXISTS call_logs (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
      twilio_call_sid TEXT UNIQUE,
      direction TEXT CHECK (direction IN ('inbound', 'outbound')),
      call_type TEXT CHECK (call_type IN ('user_initiated', 'task_callback', 'scheduled', 'third_party')),
      from_number TEXT,
      to_number TEXT,
      duration_seconds INTEGER,
      transcript TEXT,
      recording_url TEXT,
      status TEXT NOT NULL DEFAULT 'in-progress',
      started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      ended_at TIMESTAMPTZ
    );
    """)

    # Scheduled Calls — cron-based recurring or one-time calls
    db_exec("""
    CREATE TABLE IF NOT EXISTS scheduled_calls (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      label TEXT NOT NULL DEFAULT 'Scheduled call',
      cron_expression TEXT NOT NULL,
      task_context TEXT,
      timezone TEXT NOT NULL DEFAULT 'UTC',
      is_active BOOLEAN NOT NULL DEFAULT TRUE,
      last_run_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # Pending Callbacks — agent-triggered outbound calls to user
    db_exec("""
    CREATE TABLE IF NOT EXISTS pending_callbacks (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      message TEXT NOT NULL,
      allow_followup BOOLEAN NOT NULL DEFAULT TRUE,
      twilio_call_sid TEXT,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # Subscription columns on users
    db_exec("""
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS subscription_valid_until TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS subscription_auto_renew BOOLEAN DEFAULT TRUE;
    """)

    # Payments — crypto payment records
    db_exec("""
    CREATE TABLE IF NOT EXISTS payments (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      tier TEXT NOT NULL CHECK (tier IN ('pro', 'team')),
      tx_signature TEXT UNIQUE NOT NULL,
      amount_usdc BIGINT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'failed')),
      valid_until TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      confirmed_at TIMESTAMPTZ
    );
    """)

    # Add transcript accumulation column to call_logs
    db_exec("""
    ALTER TABLE call_logs
    ADD COLUMN IF NOT EXISTS transcript_json TEXT;
    """)

    # Third Party Calls — agent calls an external number autonomously
    db_exec("""
    CREATE TABLE IF NOT EXISTS third_party_calls (
      id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      agent_id UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      to_number TEXT NOT NULL,
      objective TEXT NOT NULL,
      context TEXT,
      twilio_call_sid TEXT,
      transcript TEXT,
      status TEXT NOT NULL DEFAULT 'pending',
      created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at TIMESTAMPTZ
    );
    """)

    db_exec("ALTER TABLE users ADD COLUMN IF NOT EXISTS overage_minutes INTEGER NOT NULL DEFAULT 0;")

    # Seed shared pool numbers from environment (runs on every deploy, safe to re-run)
    shared_sid = os.getenv("SHARED_NUMBER_SID", "").strip()
    shared_num = os.getenv("SHARED_NUMBER", "").strip()
    if shared_sid and shared_num:
        db_exec(
            """
            INSERT INTO phone_numbers (id, twilio_sid, number, is_shared_pool, is_dedicated)
            VALUES (gen_random_uuid(), %s, %s, TRUE, FALSE)
            ON CONFLICT (twilio_sid) DO NOTHING
            """,
            (shared_sid, shared_num),
        )
