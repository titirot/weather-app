\set ON_ERROR_STOP on

-- Connect as the configured Entra administrator group or deployment principal.
-- psql variables: runtime_role (identity name), runtime_object_id (principal object ID).
BEGIN;
SELECT pg_advisory_xact_lock(727274002);
CREATE TEMP TABLE bootstrap_identity ON COMMIT DROP AS
SELECT :'runtime_role'::text AS role_name, :'runtime_object_id'::uuid AS object_id;
DO $verify$
DECLARE
    requested bootstrap_identity%ROWTYPE;
BEGIN
    SELECT * INTO requested FROM bootstrap_identity;
    IF EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = requested.role_name
            AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)
    ) THEN
        RAISE EXCEPTION 'Runtime role must not have server administration privileges';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = requested.role_name)
       AND NOT EXISTS (
           SELECT 1 FROM pg_catalog.pgaadauth_list_principals(false)
           WHERE rolename = requested.role_name AND objectid::uuid = requested.object_id
               AND isadmin = 0
       ) THEN
        RAISE EXCEPTION 'Existing runtime role has an unexpected Entra mapping or administrator privileges';
    END IF;
END
$verify$;
SELECT pg_catalog.pgaadauth_create_principal_with_oid(
    :'runtime_role', :'runtime_object_id', 'service', false, false
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'runtime_role');
COMMIT;

\connect weather
BEGIN;
REVOKE ALL ON DATABASE weather FROM PUBLIC;
GRANT CONNECT ON DATABASE weather TO :"runtime_role";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"runtime_role";
COMMIT;
