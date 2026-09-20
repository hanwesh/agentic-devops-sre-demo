\set ON_ERROR_STOP on
\set ECHO none
\getenv runtime_role MIGRATION_RUNTIME_ROLE

-- Run as this database's migrator AFTER alembic upgrade head (or use scripts.migrate).
BEGIN;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.tasks TO :"runtime_role";
GRANT SELECT ON TABLE public.alembic_version TO :"runtime_role";
COMMIT;
