#!/usr/bin/env bash
set -euo pipefail

: "${PGHOST:?PGHOST is required}"
: "${PGDATABASE:?PGDATABASE is required}"
: "${PGUSER:?PGUSER is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"
: "${POSTGRES_APP_USER:?POSTGRES_APP_USER is required}"
: "${POSTGRES_APP_PASSWORD:?POSTGRES_APP_PASSWORD is required}"

if [[ "${PGUSER}" == "${POSTGRES_APP_USER}" ]]; then
  echo "数据库对象所有者与应用运行账号必须不同" >&2
  exit 2
fi

psql --no-psqlrc --set=ON_ERROR_STOP=1 \
  --set=owner_role="${PGUSER}" \
  --set=app_role="${POSTGRES_APP_USER}" \
  --set=app_password="${POSTGRES_APP_PASSWORD}" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'app_role', :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_role')
\gexec

SELECT format(
  'ALTER ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS',
  :'app_role', :'app_password'
)
\gexec
SELECT format('ALTER ROLE %I SET row_security = on', :'app_role')
\gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role')
\gexec
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'app_role')
\gexec
SELECT format(
  'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I',
  :'app_role'
)
\gexec
SELECT format(
  'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I',
  :'app_role'
)
\gexec
SELECT format('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO %I', :'app_role')
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'owner_role', :'app_role'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
  'GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'owner_role', :'app_role'
)
\gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
  'GRANT EXECUTE ON FUNCTIONS TO %I',
  :'owner_role', :'app_role'
)
\gexec
SQL
