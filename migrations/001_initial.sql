CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS index_metadata (
    key text PRIMARY KEY,
    value text NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    content text NOT NULL,
    contact text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS profile_chunks (
    id uuid PRIMARY KEY,
    profile_id uuid NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    text text NOT NULL,
    embedding vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS profile_chunks_owner ON profile_chunks(profile_id);

CREATE TABLE IF NOT EXISTS projects (
    id uuid PRIMARY KEY,
    slug text NOT NULL UNIQUE,
    content text NOT NULL,
    contact text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS project_chunks (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text text NOT NULL,
    embedding vector(384) NOT NULL
);
CREATE INDEX IF NOT EXISTS project_chunks_owner ON project_chunks(project_id);
