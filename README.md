# Agentic DevOps and SRE demo

A Python 3.12 FastAPI task API and a **human-governed incident-to-fix demonstration**:
Azure Monitor alert -> Azure SRE Agent investigation -> actionable GitHub issue ->
opt-in Copilot cloud-agent handoff -> reviewed fix -> gated deployment -> verified
recovery evidence.

The repository supplies application code, infrastructure, workflows, operator
tools, and mocked integration tests. It does **not** claim that cloning or deploying
the web app configures SRE connectors, grants consent, enables Copilot, or protects
GitHub environments. Those are explicit prerequisites. No agent merges its own fix
or makes unattended production changes.

## What is delivered

| Area | Implementation |
|---|---|
| Application | Async SQLAlchemy task CRUD; structured exception/correlation telemetry; immutable artifact commit identity |
| Database | Committed initial Alembic migration; PostgreSQL advisory locking; migration-built SQLite tests and fresh PostgreSQL integration tests |
| Health | `/live` for the process; `/ready` and `/health` for database, actual task columns, migration revision, and deployment identity |
| Delivery | CI gates the exact default-branch artifact; serialized staging CRUD, approved production migration/swap, read-only verification, conditional app rollback |
| Isolation | Separate production, staging, and optional demo databases/settings/telemetry on one PostgreSQL server and App Service plan |
| Incident loop | Supported, opt-in user-token Copilot API handoff; trusted structured incidents; durable evidence and explicit pending/failure states |
| Demonstration | Healthy baseline; safeguarded disposable regression branch and demo slot; bounded traffic; positive regression test retained for the fix |
| Operations | Opt-in SRE Agent IaC, operator runbook, configuration/live-read-only preflight, cost and teardown guidance |

## Local development

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -c requirements.lock

# Supply DATABASE_URL for your disposable local PostgreSQL database.
# Use the postgresql+asyncpg scheme; do not commit credentials.
alembic upgrade head
uvicorn src.main:app --reload
```

The application never calls `create_all()` at startup. A fresh database must be
migrated before readiness succeeds. Azure additionally requires TLS and separately
scoped runtime/migrator users; see [Azure setup](docs/azure-setup.md).

```bash
ruff check .
ruff format --check .
mypy src/ scripts/ --ignore-missing-imports
pytest -m "not postgres" -v
pip-audit --disable-pip --no-deps -r requirements.lock
```

`requirements.lock` pins the complete runtime dependency graph. CI installs the
same constraints, and deployment packaging uses the lock as its requirements file.
The audit disables dependency resolution only because every runtime dependency is
already pinned in that file; it does not ignore findings.

PostgreSQL integration tests create and remove only uniquely named `sre_test_*`
databases on an **explicitly authorized disposable server**:

```bash
# Set TEST_POSTGRES_URL to the ephemeral server's maintenance database.
export ALLOW_EPHEMERAL_POSTGRES=true
pytest -m postgres -v
```

Without `TEST_POSTGRES_URL`, local PostgreSQL tests are reported as skipped, not
passed. CI and the Copilot setup workflow supply an ephemeral PostgreSQL 16 service.
Do not point these tests at a shared or production server.

## HTTP contracts

| Method | Path | Behavior |
|---|---|---|
| GET | `/live` | Process liveness and public build/environment identity; no database access |
| GET | `/ready`, `/health` | `200` only when ready; `503` for unavailable/incompatible schema or missing deployed build identity |
| GET | `/docs` | OpenAPI UI |
| GET / POST | `/api/tasks` | Paginated list / create |
| GET / PUT / DELETE | `/api/tasks/{id}` | Read / update / delete |

`/health` retains its original fields and adds commit/schema/scenario metadata.
**Unhealthy responses intentionally change from HTTP 200 to 503.** Consumers must
validate the body as well as the status. Local un-packaged builds identify their
commit as `unknown`; this is not accepted for deployed-environment readiness.

The old always-broken `filter=broken` path is no longer a normal production fault.
It is rejected unless the explicitly enabled `demo` scenario is configured with
a run ID. In the healthy baseline it succeeds. The operator introduces a real,
tested missing-fallback regression only on a disposable `demo/<run-id>` branch.

## Deploying and presenting

1. Follow [Azure setup](docs/azure-setup.md): opt-in provisioning, private-network
   runner, database roles/secrets, environment-scoped OIDC, and environment reviewers.
2. Configure the separate SRE Agent, telemetry/GitHub connectors, consent and
   response plan using [SRE setup](docs/sre-agent-setup.md). Keep automated handoff
   disabled until its policy, user-token authentication and issue-origin checks pass.
3. Follow the [presenter guide](demo/README.md) for an isolated regression, bounded
   traffic, a real issue/task/PR, human review, repeatable recovery, and reset.

`ENABLE_AZURE_DEPLOY=true` opts default-branch CI into production delivery; it is
disabled when absent. Deployment cannot bypass the CI jobs, exact-commit checks,
schema/CRUD checks, or the configured production approval. The demo workflow never
swaps into production. No traffic tool should target production.

Local tests, mocked APIs, and Bicep compilation are **not** an end-to-end Azure
demonstration. Region/model availability, permissions, connectors, telemetry
ingestion, preview Copilot APIs and actual deployment/recovery require the documented
operator verification with credentials and a budget.

## Documentation

- [Architecture and trust boundaries](docs/architecture.md)
- [Azure provisioning, databases, delivery, costs and teardown](docs/azure-setup.md)
- [SRE response plan, Copilot handoff and evidence](docs/sre-agent-setup.md)
- [Repeatable presenter workflow](demo/README.md)

MIT license.
