-- Run this in your Supabase SQL editor to initialize the database

-- Agent profiles: named voice + model + prompt configs per campaign or call
CREATE TABLE IF NOT EXISTS agent_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL UNIQUE,
    system_prompt TEXT NOT NULL,
    initial_greeting TEXT NOT NULL DEFAULT 'Greet the user.',
    model TEXT NOT NULL DEFAULT 'gemini-2.0-flash-exp',
    voice TEXT NOT NULL DEFAULT 'Puck',
    language TEXT NOT NULL DEFAULT 'en-US',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Contacts (CRM)
CREATE TABLE IF NOT EXISTS contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT,
    phone TEXT NOT NULL UNIQUE,
    email TEXT,
    tags TEXT[] DEFAULT '{}',
    notes TEXT DEFAULT '',
    ai_memory TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Campaigns
CREATE TABLE IF NOT EXISTS campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    agent_profile_id UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
    schedule_type TEXT NOT NULL DEFAULT 'once',   -- once | daily | weekdays
    scheduled_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | running | completed | paused
    total_contacts INTEGER DEFAULT 0,
    called INTEGER DEFAULT 0,
    answered INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Campaign contacts (contacts assigned to a campaign)
CREATE TABLE IF NOT EXISTS campaign_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id UUID REFERENCES contacts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | called | answered | failed
    called_at TIMESTAMPTZ,
    UNIQUE(campaign_id, contact_id)
);

-- Call logs
CREATE TABLE IF NOT EXISTS call_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
    campaign_id UUID REFERENCES campaigns(id) ON DELETE SET NULL,
    agent_profile_id UUID REFERENCES agent_profiles(id) ON DELETE SET NULL,
    phone TEXT NOT NULL,
    room_name TEXT,
    direction TEXT NOT NULL DEFAULT 'outbound',
    status TEXT NOT NULL DEFAULT 'initiated',  -- initiated | answered | no_answer | failed | completed
    duration_seconds INTEGER DEFAULT 0,
    recording_url TEXT,
    transcript TEXT,
    ai_summary TEXT,
    started_at TIMESTAMPTZ DEFAULT now(),
    ended_at TIMESTAMPTZ
);

-- Appointments
CREATE TABLE IF NOT EXISTS appointments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
    call_log_id UUID REFERENCES call_logs(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    scheduled_at TIMESTAMPTZ NOT NULL,
    notes TEXT DEFAULT '',
    calcom_booking_id TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',   -- scheduled | completed | cancelled
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Settings (BYOK: Bring Your Own Keys — stored per-row, keyed by name)
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Seed a default agent profile
INSERT INTO agent_profiles (name, system_prompt, initial_greeting, model, voice, language)
VALUES (
    'Default Agent',
    'You are Aiona, a friendly and professional AI voice assistant making an outbound call. Your goal is to assist the person and, if appropriate, schedule an appointment. Be concise, warm, and professional. When the person agrees to a meeting, use the book_appointment tool.',
    'Hello! This is Aiona calling. How are you doing today?',
    'gemini-2.0-flash-exp',
    'Puck',
    'en-US'
) ON CONFLICT (name) DO NOTHING;
