-- Add session_id column for linking thoughts from the same brain dump
ALTER TABLE memories ADD COLUMN IF NOT EXISTS session_id UUID DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_memories_session_id ON memories(session_id) WHERE session_id IS NOT NULL;
