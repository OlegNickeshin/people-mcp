-- Authorization state is private and is never embedded or returned in search.
CREATE TABLE IF NOT EXISTS oauth_publishers (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id text PRIMARY KEY,
    metadata jsonb NOT NULL,
    secret_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_browser_sessions (
    token_hash text PRIMARY KEY,
    publisher_id uuid NOT NULL REFERENCES oauth_publishers(id) ON DELETE CASCADE,
    expires_at bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_grants (
    id uuid PRIMARY KEY,
    publisher_id uuid NOT NULL REFERENCES oauth_publishers(id) ON DELETE CASCADE,
    client_id text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    resource text NOT NULL,
    scopes text[] NOT NULL,
    revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    flow_hash text PRIMARY KEY,
    client_id text NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    params jsonb NOT NULL,
    expires_at bigint NOT NULL,
    csrf_hash text,
    code_hash text UNIQUE,
    publisher_id uuid REFERENCES oauth_publishers(id) ON DELETE CASCADE,
    grant_id uuid REFERENCES oauth_grants(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oauth_tokens (
    token_hash text PRIMARY KEY,
    grant_id uuid NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('access', 'refresh')),
    expires_at bigint NOT NULL,
    used boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS oauth_tokens_grant ON oauth_tokens(grant_id);
CREATE INDEX IF NOT EXISTS oauth_pending_expiry ON oauth_pending(expires_at);
CREATE INDEX IF NOT EXISTS oauth_sessions_expiry ON oauth_browser_sessions(expires_at);
CREATE TABLE IF NOT EXISTS publication_owners (
    publisher_id uuid NOT NULL REFERENCES oauth_publishers(id),
    profile_id uuid UNIQUE REFERENCES profiles(id) ON DELETE CASCADE,
    project_id uuid UNIQUE REFERENCES projects(id) ON DELETE CASCADE,
    CHECK ((profile_id IS NULL) <> (project_id IS NULL))
);
CREATE INDEX IF NOT EXISTS publication_owners_publisher ON publication_owners(publisher_id);
