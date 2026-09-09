-- Add a third publication kind without changing existing publications or vectors.
CREATE TABLE IF NOT EXISTS agents (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    content text NOT NULL,
    contact text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_chunks (
    id uuid PRIMARY KEY,
    agent_id uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    text text NOT NULL,
    embedding vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_chunks_owner ON agent_chunks(agent_id);

ALTER TABLE publication_owners
    ADD COLUMN IF NOT EXISTS agent_id uuid UNIQUE REFERENCES agents(id) ON DELETE CASCADE;
-- Replace the old two-kind XOR, retaining exactly one publication per ownership row.
ALTER TABLE publication_owners DROP CONSTRAINT IF EXISTS publication_owners_check;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'publication_owners'::regclass
          AND conname = 'publication_owners_one_kind_check'
    ) THEN
        ALTER TABLE publication_owners ADD CONSTRAINT publication_owners_one_kind_check
            CHECK (num_nonnulls(profile_id, project_id, agent_id) = 1);
    END IF;
END $$;
