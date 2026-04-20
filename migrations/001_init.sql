CREATE TABLE IF NOT EXISTS users (
    telegram_id         BIGINT PRIMARY KEY,
    username            TEXT,
    default_mode        TEXT NOT NULL DEFAULT 'full'
        CHECK (default_mode IN ('transcript', 'short', 'medium', 'full')),
    default_output_lang TEXT NOT NULL DEFAULT 'auto'
        CHECK (default_output_lang IN ('auto', 'ru', 'en', 'es')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
