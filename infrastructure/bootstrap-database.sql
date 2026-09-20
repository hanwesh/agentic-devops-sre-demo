\set ON_ERROR_STOP on
\set ECHO none
\getenv database PGDATABASE
\getenv runtime_role BOOTSTRAP_RUNTIME_ROLE
\getenv migrator_role BOOTSTRAP_MIGRATOR_ROLE
\getenv runtime_password BOOTSTRAP_RUNTIME_PASSWORD
\getenv migrator_password BOOTSTRAP_MIGRATOR_PASSWORD

BEGIN;
-- Fail rather than taking over or rotating an existing role.
CREATE ROLE :"runtime_role" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION PASSWORD :'runtime_password';
CREATE ROLE :"migrator_role" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION PASSWORD :'migrator_password';

REVOKE ALL ON DATABASE :"database" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database" TO :"runtime_role", :"migrator_role";
GRANT USAGE ON SCHEMA public TO :"runtime_role";
GRANT USAGE, CREATE ON SCHEMA public TO :"migrator_role";

-- The migrator grants only the application tables after a successful upgrade.
COMMIT;
