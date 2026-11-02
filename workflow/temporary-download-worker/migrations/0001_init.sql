-- Store only a SHA-256 hash of each secret download token.
-- Keep the R2 bucket private; objects are never exposed by a public R2 URL.
CREATE TABLE IF NOT EXISTS apk_objects (
    object_key TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK (size > 0 AND size <= 524288000),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    r2_etag TEXT NOT NULL,
    filename TEXT NOT NULL,
    registered_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS download_tokens (
    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    request_id TEXT NOT NULL UNIQUE,
    object_key TEXT NOT NULL REFERENCES apk_objects(object_key),
    kind TEXT NOT NULL CHECK (kind IN ('canary', 'recipient', 'expiry')),
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    revoked_at INTEGER,
    CHECK (expires_at = issued_at + 3600000)
);

CREATE INDEX IF NOT EXISTS idx_tokens_expiry ON download_tokens(expires_at);
