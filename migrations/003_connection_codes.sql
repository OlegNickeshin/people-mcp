-- Private, short-lived connection credentials; never part of the public index.
CREATE TABLE IF NOT EXISTS oauth_connection_codes (
    code_hash text PRIMARY KEY,
    publisher_id uuid NOT NULL REFERENCES oauth_publishers(id) ON DELETE CASCADE,
    grant_id uuid NOT NULL REFERENCES oauth_grants(id) ON DELETE CASCADE,
    expires_at bigint NOT NULL,
    used_at bigint
);
CREATE INDEX IF NOT EXISTS oauth_connection_codes_publisher ON oauth_connection_codes(publisher_id);
CREATE TABLE IF NOT EXISTS oauth_connection_limits (
    publisher_id uuid NOT NULL REFERENCES oauth_publishers(id) ON DELETE CASCADE,
    action text NOT NULL CHECK (action IN ('issue', 'redeem')),
    window_start bigint NOT NULL,
    attempts integer NOT NULL,
    PRIMARY KEY (publisher_id, action)
);
ALTER TABLE oauth_pending ADD COLUMN IF NOT EXISTS link_attempts integer NOT NULL DEFAULT 0;
