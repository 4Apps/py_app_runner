-- API keys.
--
-- A key is `<key_prefix>.<secret>`. Only sha256(api_key_pepper || secret) is stored; the
-- full key is printed once by `api_keys create` and cannot be recovered.

CREATE TABLE api_keys (
    id           bigserial PRIMARY KEY,
    name         text NOT NULL,
    key_prefix   text NOT NULL UNIQUE,
    secret_hash  text NOT NULL,

    -- 'service:action', 'service:*' or '*'. Empty allows nothing.
    abilities    text[] NOT NULL DEFAULT '{}',

    -- NULL allows any address.
    allowed_ips  cidr[],

    expires_at   timestamptz,

    -- The user this key acts as when a request carries no JWT. Add the foreign key to your
    -- users table yourself, e.g.:
    --   ALTER TABLE api_keys ADD FOREIGN KEY (user_id) REFERENCES users (id);
    user_id      bigint,

    last_used_at timestamptz,
    total_uses   bigint NOT NULL DEFAULT 0,

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    disabled_at  timestamptz
);
