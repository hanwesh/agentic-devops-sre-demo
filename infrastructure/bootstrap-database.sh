#!/usr/bin/env bash
# One-time bootstrap from an operator host with private PostgreSQL access. Never CI runtime.
set -euo pipefail
set +x

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ "${1:-}" != "--apply" ]]; then
  echo "Offline only. Set the documented BOOTSTRAP_*/PG* environment variables."
  echo "Then explicitly use: $0 --apply --confirm-database taskdb_<environment>"
  exit 0
fi
: "${BOOTSTRAP_ENVIRONMENT:?Set production, staging, or demo}"
: "${PGHOST:?Set the private PostgreSQL FQDN}"
: "${PGUSER:?Set the bootstrap administrator, not a runtime user}"
: "${PGPASSWORD:?Supply the bootstrap credential via a protected environment}"
: "${PGDATABASE:?Set the exact environment database}"
: "${BOOTSTRAP_DATABASE_NAME:?Set the approved logical database from the IaC outputs}"
: "${BOOTSTRAP_RUNTIME_PASSWORD:?Supply a distinct runtime password}"
: "${BOOTSTRAP_MIGRATOR_PASSWORD:?Supply a distinct migration password}"
case "$BOOTSTRAP_ENVIRONMENT" in
  production|staging|demo) ;;
  *) echo "Invalid bootstrap environment." >&2; exit 2 ;;
esac
[[ "$#" == 3 && "$2" == "--confirm-database" && "$3" == "$PGDATABASE" &&
   "$PGDATABASE" == "$BOOTSTRAP_DATABASE_NAME" &&
   "$PGDATABASE" =~ ^[a-z][a-z0-9_]{0,62}$ &&
   "$PGDATABASE" != postgres && "$PGDATABASE" != template0 && "$PGDATABASE" != template1 ]] || {
  echo "Exact environment database confirmation required." >&2; exit 2;
}
[[ "$BOOTSTRAP_RUNTIME_PASSWORD" != "$BOOTSTRAP_MIGRATOR_PASSWORD" &&
   "$PGPASSWORD" != "$BOOTSTRAP_RUNTIME_PASSWORD" &&
   "$PGPASSWORD" != "$BOOTSTRAP_MIGRATOR_PASSWORD" ]] || {
  echo "Administrator, runtime, and migrator credentials must differ." >&2; exit 2;
}
export BOOTSTRAP_RUNTIME_ROLE="task_${BOOTSTRAP_ENVIRONMENT}_runtime"
export BOOTSTRAP_MIGRATOR_ROLE="task_${BOOTSTRAP_ENVIRONMENT}_migrator"
export PGSSLMODE=verify-full
psql --no-psqlrc --no-password --quiet --set=ON_ERROR_STOP=1 \
  --file="$ROOT/infrastructure/bootstrap-database.sql"
echo "Scoped roles created. Run environment-scoped migrations, then grant application DML."
