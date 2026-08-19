-- User groups: cohorts/personas backing the homepage password picker, the
-- admin group-management page, and the per-group "visible" toggle
-- (feature merged in PR "DB-backed group store + homepage password picker").
--
-- The app's init_user_groups_table() also creates this table at runtime and
-- seeds the rows with pbkdf2-hashed passwords (hashing needs app-side code +
-- env vars, so seeding stays in the app). This migration provisions the schema
-- for staging/production via the migration workflow so deployed environments
-- never depend on first-load DDL. Schema kept identical to the app's
-- _CREATE_TABLE_SQL so whichever runs first wins and the other is a no-op.
CREATE TABLE IF NOT EXISTS public.user_groups (
    slug          VARCHAR(64)  PRIMARY KEY,
    label         VARCHAR(128) NOT NULL,
    icon          VARCHAR(16)  NOT NULL DEFAULT '',
    color         VARCHAR(16)  NOT NULL DEFAULT '',
    priority      INTEGER      NOT NULL DEFAULT 0,
    password_hash TEXT,
    is_admin      BOOLEAN      NOT NULL DEFAULT FALSE,
    visible       BOOLEAN      NOT NULL DEFAULT TRUE,
    chart_color   VARCHAR(16)  NOT NULL DEFAULT '',
    chart_label   VARCHAR(64)  NOT NULL DEFAULT '',
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- Forward-compat if an earlier app build created the table before `visible`.
ALTER TABLE public.user_groups ADD COLUMN IF NOT EXISTS visible BOOLEAN NOT NULL DEFAULT TRUE;
