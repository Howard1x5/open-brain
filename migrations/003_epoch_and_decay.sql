-- Migration 003: Epoch tagging + query-time confidence decay
--
-- Prevents stale infrastructure facts from polluting search results
-- as the memory database grows across infrastructure changes.
--
-- Applied: 2026-04-12

-- 1. Add epoch column to tag memories by infrastructure era
ALTER TABLE memories ADD COLUMN IF NOT EXISTS epoch VARCHAR(30) DEFAULT 'proxmox-v2';

-- 2. Index for efficient epoch filtering
CREATE INDEX IF NOT EXISTS memories_epoch_idx ON memories USING btree (epoch);

-- 3. Query-time confidence decay function
-- Category-aware half-lives:
--   admin/general (infrastructure): 60-day half-life (fast decay)
--   project: 120-day half-life (medium decay)
--   idea/decision/person: 180-day half-life (slow decay)
-- Floor of 0.05 — nothing ever fully disappears
CREATE OR REPLACE FUNCTION effective_confidence(
    base_confidence DOUBLE PRECISION,
    created_at TIMESTAMPTZ,
    category VARCHAR,
    decay_fast_days INTEGER DEFAULT 60,
    decay_slow_days INTEGER DEFAULT 180
)
RETURNS DOUBLE PRECISION
LANGUAGE plpgsql
IMMUTABLE
AS $function$
DECLARE
    age_days DOUBLE PRECISION;
    half_life DOUBLE PRECISION;
    decay_factor DOUBLE PRECISION;
BEGIN
    age_days := EXTRACT(EPOCH FROM (NOW() - created_at)) / 86400.0;

    -- Category-based half-life
    half_life := CASE
        WHEN category IN ('admin', 'general') THEN decay_fast_days
        WHEN category = 'project' THEN 120
        ELSE decay_slow_days
    END;

    -- Exponential decay: confidence * 2^(-age/half_life)
    decay_factor := POWER(2.0, -(age_days / half_life));

    RETURN GREATEST(COALESCE(base_confidence, 0.8) * decay_factor, 0.05);
END;
$function$;

-- 4. Backfill: tag pre-migration memories with the previous epoch
-- Run manually per-deployment — adjust the cutoff timestamp to match
-- your migration date:
--
--   UPDATE memories SET epoch = 'proxmox-v1'
--   WHERE created_at < '2026-04-08T10:00:00+00';
--
-- Future epoch transitions:
--   ALTER TABLE memories ALTER COLUMN epoch SET DEFAULT 'new-epoch-name';
--   -- Old data keeps its tag. New data gets the new tag.
