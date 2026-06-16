-- Generalize the users table so the same schema serves Telegram and WhatsApp.
-- Identity becomes (platform, external_id): a Telegram user id (as text) or a
-- WhatsApp wa_id (phone number, digits only). Existing Telegram rows are
-- migrated in place — telegram_id is kept for back-reference but no longer used
-- as the key.

ALTER TABLE users ADD COLUMN IF NOT EXISTS platform    TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_id TEXT;

-- Backfill existing rows (all of which are Telegram users).
UPDATE users
   SET platform = 'telegram'
 WHERE platform IS NULL;

UPDATE users
   SET external_id = telegram_id::text
 WHERE external_id IS NULL
   AND telegram_id IS NOT NULL;

-- telegram_id is no longer the primary key and is absent for WhatsApp rows.
-- Drop the primary key FIRST — Postgres won't let a PK column become nullable.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_pkey;
ALTER TABLE users ALTER COLUMN telegram_id DROP NOT NULL;

ALTER TABLE users ALTER COLUMN platform    SET NOT NULL;
ALTER TABLE users ALTER COLUMN external_id SET NOT NULL;

-- New identity / upsert key.
CREATE UNIQUE INDEX IF NOT EXISTS users_platform_external_uq
    ON users (platform, external_id);
