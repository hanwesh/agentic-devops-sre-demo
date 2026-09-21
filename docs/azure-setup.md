# Azure setup: isolated, approval-gated SRE demo

This is a **workshop**, not a fully autonomous production operator. Treat even the
`production` slot as synthetic workshop data. Never deploy the template into a
shared production resource group. Provisioning, secret creation, database
bootstrap, role assignments, model consent, and incident-platform setup are
operator actions. Nothing in this guide was executed against Azure for this PR.

Public schema/documentation review: **2026-09-20**. Provider availability, quota,
regional model offerings, and actual prices still require subscription-specific
verification.

## 1. What is deployed, and what is not

| Resource | Isolation and default |
|---|---|
| Linux App Service, Python 3.12, Standard S1 | One paid plan; production app and staging slot. No production traffic routing to nonproduction slots. |
| Optional `demo` slot | `enableDemoSlot=false` by default. Separate database, identity, Insights, and workspace when enabled. Same compute plan: logical isolation **not** CPU/memory isolation. Never load-test this plan. |
| PostgreSQL Flexible Server 16 | One server, private delegated subnet, public access disabled, 7-day backups, no HA. Distinct configurable logical databases default to `taskdb_production`, `taskdb_staging`, optional `taskdb_demo`. This is not an HA production design. |
| Telemetry | **Separate Application Insights and Log Analytics workspace per environment**, including demo. The agent's own telemetry is separate again if enabled. |
| Alerts | Two demo-only scheduled query rules and an email action group. Rules are disabled until `enableDemoAlerts=true` and the operator has validated telemetry/receiver setup. No default production alerts. |
| Optional SRE Agent | `enableSreAgent=false`. Published `Microsoft.App/agents@2026-01-01`; `ReadOnly`/`Low`, separate system and user identities. Optional **separate** `grantSreInvestigationRoles` flag grants demo-resource readers only. |
| Existing Key Vault secrets | Referenced, not created or output. Runtime credentials never use the PostgreSQL administrator. |
| Not deployed | GitHub runner compute, private-network peering/enterprise egress, Key Vaults, secrets, DB login roles/schema, OIDC registrations/federation, GitHub protections, Copilot subscriptions, source-code consent, SRE user roles/connectors/incident plans, budgets, or usage limits. |

Published resource versions are pinned in the Bicep files. The old
[`Microsoft.App/sreagents` reference](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/sreagents)
returned **404** on the review date. The supported replacement reference is
[`Microsoft.App/agents`](https://learn.microsoft.com/en-us/azure/templates/microsoft.app/agents).
Do not replace it with a guessed `sreAgents` type or copy undocumented preview
properties. See [SRE Agent setup](#7-optional-sre-agent-and-operator-boundaries).

## 2. Prerequisites and private networking

Prepare these **before** attempting application deployment:

1. Python 3.12, project dependencies, Azure CLI and Bicep, GitHub CLI for optional
   GitHub checks, and **psql 15+** for the one-time `\getenv` bootstrap script.
2. A dedicated subscription/resource-group scope with approved spend and quotas
   for S1, PostgreSQL, and monitoring. Use a unique lowercase `baseName` of 3–24
   characters and a group named `rg-sre-demo-<unique>`. Helpers reject empty,
   default, shared, and production-labelled group names.
3. Register `Microsoft.Web`, `Microsoft.DBforPostgreSQL`,
   `Microsoft.OperationalInsights`, `Microsoft.Insights`, `Microsoft.Network`,
   and `Microsoft.ManagedIdentity`. Register `Microsoft.App` **only if** opting
   into SRE Agent. Registration is not part of preflight.
4. Review the nonoverlapping VNet/subnet CIDRs. The template defaults are
   `10.42.0.0/16`, apps `10.42.1.0/24` delegated to
   `Microsoft.Web/serverFarms`, and PostgreSQL `10.42.2.0/24` delegated to
   `Microsoft.DBforPostgreSQL/flexibleServers`. The private zone
   `<baseName>.postgres.database.azure.com` is linked to the VNet. All slots
   integrate with the apps subnet; `vnetRouteAllEnabled=true`.
5. Supply an **existing, trusted, ephemeral Actions runner** network path to this
   VNet (or an approved GitHub-hosted runner private-network integration). No
   persistent runner VM or NAT gateway is purchased by this template. A generic
   `ubuntu-latest` runner on the public Internet cannot reach this database.
   A runner must not use the delegated apps or PostgreSQL subnets for its compute.
6. Arrange peering/VPN and private DNS resolution/forwarding for the operator and
   runner network; verify the server FQDN resolves privately and TCP 5432 is
   reachable. Route/NSG policy must permit necessary Azure PostgreSQL service
   traffic. Restrict enterprise egress deliberately, allowing the required Azure
   identity/management/telemetry, GitHub/SCM, and package endpoints. Do not open
   PostgreSQL's Azure-all `0.0.0.0` firewall exception as a workaround.
7. Allow each slot's network and **own managed identity** to read its existing
   runtime Key Vault secret. Separate environment vaults are recommended.
   For network-restricted vaults, permit the integration subnet using the approved
   service endpoint or private endpoint/DNS design. The template enables the
   Key Vault service endpoint but does not change an existing vault's firewall.

Runner groups must restrict use to this repository and trusted deployment
workflows. Prefer separate runner groups/labels and OIDC identities for
staging, production, and demo. Ephemeral runners must be fresh per job and
destroyed after it; do not put PR code or untrusted fork jobs on a runner holding
production secrets/network access. Runner access is an explicit preflight
attestation, not a claim that a label creates network connectivity.

Sources: [PostgreSQL private networking](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/concepts-networking-private),
[App Service VNet integration](https://learn.microsoft.com/en-us/azure/app-service/overview-vnet-integration),
[Key Vault references](https://learn.microsoft.com/en-us/azure/app-service/app-service-key-vault-references),
[ephemeral self-hosted runners](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/autoscaling-with-self-hosted-runners).

## 3. Parameter preparation and guarded provisioning

Copy `infrastructure/parameters.json` to an operator-controlled, untracked file
**inside the checkout**. Replace every placeholder. Never put passwords/tokens in
this file or pass them on a CLI command line.

Required inputs:

- `baseName`, the intended region (the wrapper supplies `location`), and
  `postgresAdminLogin`.
- `productionDatabaseName`, `stagingDatabaseName`, and `demoDatabaseName`:
  distinct non-system lowercase SQL identifiers. Keep the IaC outputs, bootstrap,
  runtime secret URLs, `DATABASE_NAME`, and protected job variables aligned.
- `postgresAdminPassword`: an **ARM Key Vault parameter reference** to an
  existing bootstrap-administrator secret, never a literal `value`.
  The vault must permit ARM template secret retrieval, and the deployer must
  have the corresponding `Microsoft.KeyVault/vaults/deploy/action` access.
  This is different from an app's Key Vault reference permission.
- `productionDatabaseSecretUri` and `stagingDatabaseSecretUri`: distinct,
  **versioned** URIs, e.g.
  `https://<environment-vault>.vault.azure.net/secrets/runtime-database-url/<version>`.
  With demo enabled, also supply `demoDatabaseSecretUri`. Only URIs, not values,
  enter the parameters file.
- Keep all opt-ins false until prerequisites are satisfied. Enabling SRE also
  requires demo, explicit `sreAgentModelName`, `sreAgentModelProvider`, and a
  nonsecret `sreAgentConsentReference`. That reference records human review; it
  does **not** give OAuth/model consent.

The administrator and three runtime/migrator credential pairs are supplied by
the operator's credential-management process, not generated by these scripts.
When secrets depend on a newly created server, prepare the secret references,
provision infrastructure, then bootstrap the accounts and populate/verify the
references **before deploying application code**. An unresolved reference is
not a healthy app, and `/ready` must fail closed.

Run the offline checks first:

```bash
az bicep build --file infrastructure/main.bicep --stdout > /dev/null
bash infrastructure/deploy.sh \
  --resource-group rg-sre-demo-<unique> \
  --subscription <subscription-uuid> --location eastus2 \
  --parameters infrastructure/operator.parameters.json
```

The helper defaults to a **local plan**, without Azure calls. It validates
parameter structure and conditional opt-ins but cannot verify secret existence,
RBAC, budget, quota, regional SKU availability, or provider consent. Shell
placeholders in this guide must be replaced before use.

An authorized operator creates/approves the dedicated group separately and tags
it `purpose=agentic-devops-sre-demo`, `deployment=<exact-group-name>`. Add
`disposable=true` **only** for a group whose entire contents may later be deleted.
The helper does not create a group or broaden existing access.

To request a **live** resource-ID-only what-if, append:

```text
--what-if --confirm-resource-group rg-sre-demo-<unique>
```

After separately reviewing it, use `--apply` instead of `--what-if`.
Both require the exact group confirmation, matching ownership tags, and an
inventory containing no unowned/shared resource. Deployments
use **Incremental** mode, never Complete mode. Disabling an opt-in on a later
incremental deployment does **not** delete already-created resources. App setting
deployment resets demo flags to false and the run ID to empty; never redeploy
infrastructure in the middle of a scenario.

Do not use `--debug`, shell tracing, full deployment dumps, or raw appsetting
exports in CI logs. Outputs contain resource IDs, identities, database names and
hostnames only; no connection strings or credentials. What-if is a live ARM
operation, not the default offline preflight.

Source: [Key Vault parameter references](https://learn.microsoft.com/en-us/azure/azure-resource-manager/templates/key-vault-parameter).

## 4. Database bootstrap and noninteractive migrations

### Identities and grants

| Environment | Database | Runtime login | Migrator login |
|---|---|---|---|
| production | `taskdb_production` | `task_production_runtime` | `task_production_migrator` |
| staging | `taskdb_staging` | `task_staging_runtime` | `task_staging_migrator` |
| demo (optional) | `taskdb_demo` | `task_demo_runtime` | `task_demo_migrator` |

The administrator is used only to bootstrap. Runtime and migrator roles are
distinct, non-superusers, without `CREATEDB`, `CREATEROLE`, or replication
privileges. `PUBLIC` loses database privileges and public-schema privileges in
**each** database. Only the matching logins receive `CONNECT`. Runtime receives
schema `USAGE` and app-table DML; only the migrator receives
schema `CREATE` and owns its migration-created objects. Runtime cannot alter the
schema or update `alembic_version`.

The bootstrap is deliberately **one-time**: existing role names cause a
transaction rollback, not credential rotation or takeover of an existing role.
Run it for every created environment before allowing any workload traffic.
Review inherited memberships/existing grants if adapting an existing server.

### One-time operator action

On a host with the private DNS/network path, supply these variables from a
protected credential mechanism (do not paste real values into commands, shell
history, a committed `.env`, or logs):

```text
BOOTSTRAP_ENVIRONMENT=staging
BOOTSTRAP_DATABASE_NAME=taskdb_staging
PGHOST=<baseName>-pg.postgres.database.azure.com
PGDATABASE=taskdb_staging
PGUSER=<bootstrap administrator>
PGPASSWORD=<bootstrap administrator password from protected environment>
BOOTSTRAP_RUNTIME_PASSWORD=<distinct staging runtime password>
BOOTSTRAP_MIGRATOR_PASSWORD=<distinct staging migrator password>
```

`bash infrastructure/bootstrap-database.sh` is an offline reminder only. To
perform the approved bootstrap, run:

```bash
bash infrastructure/bootstrap-database.sh \
  --apply --confirm-database taskdb_staging
```

The script forces TLS `verify-full`, disables psql startup customizations, reads
passwords from the environment using `\getenv`, and quotes SQL identifiers and
values separately. It creates no password file and prints no connection string.
Keep server-side audit access restricted too: provisioning SQL is sensitive.
Clear the administrator/password environment afterwards. Repeat with production
and (if enabled) demo using **different** credentials.

The runtime Key Vault secret value has the shape
`postgresql+asyncpg://task_staging_runtime:<URL-encoded-password>@<server-fqdn>:5432/taskdb_staging?ssl=verify-full`.
Do not use the administrator or migrator in `DATABASE_URL`. Percent-encode
credentials correctly; do not interpolate raw passwords into URLs.

### Migration job contract

Each protected GitHub environment supplies its own `MIGRATION_DATABASE_URL`
using its **migrator** login, plus expected database/user values. Set
`DATABASE_SSL_REQUIRED=true` in the migration job as well as the application.
Run from the environment-scoped ephemeral runner:

```bash
python -m scripts.migrate \
  --database taskdb_staging --user task_staging_migrator \
  --runtime-user task_staging_runtime
```

The helper invokes `alembic upgrade head` noninteractively using the protected
environment, not a password in argv. No `az webapp ssh --command`, undocumented
Kudu shell feature, or app-startup schema creation is needed.

After every successful upgrade, this helper grants `public.tasks` DML and
`public.alembic_version` SELECT **as that same migrator**. A failed migration or
grant exits nonzero and blocks deployment. It grants no schema creation, version
table writes, or access to future/unrelated tables. The standalone reviewed
`infrastructure/grant-runtime.sql` is an operator alternative, with
`MIGRATION_RUNTIME_ROLE` set to
the matching `task_<environment>_runtime` and psql's `PG*` variables set to the
**migrator** credentials. psql expects its own libpq connection environment, not
the SQLAlchemy `postgresql+asyncpg` URL. Do not run this grant file as runtime.
Future table migrations must carry reviewed runtime grants as well.

Staging and production do **not** share a database. Migrate staging before its
CRUD smoke test; migrate production only after production approval and before
promotion. Use additive, backward-compatible expand/contract migrations while
old and new artifacts coexist. A slot rollback rolls back **code**, not data or
schema. Do not automatically downgrade a database as rollback. Review irreversible
changes and restore/backups separately; neither a backup retention setting nor a
green unit test proves a successful restore.

## 5. Slot settings, health, and promotion

These settings stick to their environment rather than travelling with code:

| Setting | Value |
|---|---|
| `ENVIRONMENT` | `production`, `staging`, or `demo` |
| `DATABASE_URL` | Own versioned Key Vault runtime reference |
| `DATABASE_NAME` | Exact logical database from the environment's IaC output |
| `DATABASE_SSL_REQUIRED` | `true` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Own Application Insights instance |
| `OTEL_SERVICE_NAME` | `task-api` |
| `OTEL_TRACES_SAMPLER` | `always_on` for deterministic low-volume demo telemetry |
| `DEMO_SCENARIO_ENABLED` | Always `false` for production/staging; demo initially `false` |
| `DEMO_RUN_ID` | Initially empty; only the guarded demo operator workflow sets it |

Never configure a sticky commit SHA. The deployed SHA/version come from the
immutable packaged `src/build_info.json`, so identity follows the artifact across
promotion and rollback. Never swap the intentionally faulty demo slot into
production.

- `/live`: process liveness and identity; **no database operation**.
- `/ready` and `/health`: schema-aware readiness. Require the committed Alembic
  revision, queryable application columns, and known deployed commit.
- App Service health check: `/ready`.
- Restart/swap warmup: `/live`, accepting HTTP 200 only. Startup does not perform
  migrations or CRUD. A warmup success is **not** database readiness evidence.

Azure applies **target slot settings to the source slot during swap warmup**.
Do not perform staging CRUD after starting swap/preview: that process can be
using production settings. Run the staging CRUD test **before** starting the
swap, while `/ready` reports `environment=staging` and the candidate commit.
After approval/migration/promotion, use production **read-only** readiness and
endpoint checks, then verify staging's identity/secret binding is restored.
Read [swap semantics](https://learn.microsoft.com/en-us/azure/app-service/deploy-staging-slots).

Managed identities and VNet integration do not swap. Own-secret-only access
can prevent the source identity from resolving the target secret during warmup,
including rollback; an unresolved URL can prevent process startup even on `/live`.
For this synthetic workshop, an operator must explicitly review whether **both
swapping identities** may read **exactly the two runtime secrets**, never the
administrator, migrator, or demo secrets. This is a real isolation tradeoff, not
an automatic permission grant. The demo identity must remain demo-secret-only.
Validate reference resolution and forward/reverse warmup in the approved scope
before setting `SWAP_KEY_VAULT_READY_CONFIRMED=true`. Delivery stops without that
attestation. If policy requires strict own-secret-only isolation, do not enable
this swap pipeline. A direct-production alternative is **not implemented** here.
Stop for an operator-reviewed deployment design; do not weaken policy silently.
No live swap or Key Vault resolution was validated for this PR.

## 6. Request alerts and incident metadata

`infrastructure/alerts/policy.json` is the threshold contract;
`request-window.kql` and the two signal `.kql` suffixes are loaded using
`loadTextContent` in `alerts.bicep`. Changes should update tests and operator
expectations together.

Queries run against the **demo workspace's `AppRequests` table**, further
restricted to the exact demo Application Insights `_ResourceId`, service
`task-api`, and request property `environment=demo`. They exclude `/live`,
`/ready`, `/health`, including trailing slash/query-string variants.

| Rule | Over a rolling 5-minute window |
|---|---|
| `http-5xx` | `100 * represented 500–599 requests / represented requests > 5` |
| `latency-p95` | Weighted p95 of **`DurationMs`**, in milliseconds, **> 3000** |
| Both | At least **20 observed telemetry records**, evaluated every minute. Exactly 5% or 3000ms does not fire. No traffic/no eligible group produces no alert. |

`ItemCount` is the number of requests represented by each sampled record.
The ratio uses `sum(ItemCount)`/`sumif(...)`, with missing/nonpositive weights
treated as one. The percentile uses `percentilew(DurationMs, SampleWeight, 95)`.
The sample floor uses `count()`, so one heavily weighted sample cannot create an
incident. Sampling can still distort a low-volume run; keep demo SDK/ingestion
sampling off and verify it in telemetry. Kusto percentile estimates are
approximate. Python tests cover the shared policy, equations, small-fixture
nearest-rank boundaries and query structure; they do not execute KQL.

Application request spans carry the following flat properties:

```text
environment
deployment_sha
demo_run_id
correlation_id
trace_id
```

The OTel resource also carries `service.name=task-api` (exported as `AppRoleName`)
and `deployment.environment.name`. These resource attributes are not substitutes
for the flat request properties used by alerts/preflight. Validate their actual
exporter mapping in `AppRequests.Properties`, not only local log output.

Separate run/commit groups prevent two runs or deployments contaminating each
other's denominator. Five alert dimensions expose `ServiceName`, `Environment`,
`DeploymentCommit`, `DemoRunId`, and `IncidentFingerprint`; `ResourceId` identifies
the demo App Service slot. Fingerprints have the deterministic form:

```text
SHA256("demo|<run-id>|/api/tasks?filter=broken|<http_5xx-or-latency>")
```

There is no trailing newline. The fingerprint matches the incident schema and
survives a repair commit; metrics remain grouped by run/commit. Rows without a
valid run ID or full commit are excluded. Queries also emit exact `WindowStart`,
`WindowEnd`, `SampleCount`, `RequestCount`, `ErrorCount`, and `P95DurationMs`.
Common alert schema is enabled for the email
receiver, and action properties identify service/environment/signal. Correlation
IDs are bounded query evidence, not additional high-cardinality alert dimensions.
Consumers must validate metadata and obtain trusted GitHub evidence; an alert
alone must not authorize code execution or production writes.

Ingestion/export batches and scheduled evaluation introduce delay. Five minutes
is the measurement window, **not** an alert-delivery SLA. Late arrivals outside
the window can be missed. Emit a bounded run of at least 20 business requests
within five minutes, wait for ingestion, query the rendered KQL, validate receiver
delivery, then enable the rules. A new workspace may lack `AppRequests`; disabled
rules skip query validation to allow initial provisioning. Enabled rules do not
skip validation. The optional preflight telemetry probe checks the last 15
minutes for *matching metadata*, not that the five-minute alert has fired.
Do not manufacture records to make a blank dashboard appear validated.

Sources: [AppRequests schema/units/sampling](https://learn.microsoft.com/en-us/azure/azure-monitor/reference/tables/apprequests),
[weighted percentiles](https://learn.microsoft.com/en-us/kusto/query/percentilesw-aggregation-function),
[scheduled query rule schema](https://learn.microsoft.com/en-us/azure/templates/microsoft.insights/2023-12-01/scheduledqueryrules),
[common alert schema](https://learn.microsoft.com/en-us/azure/azure-monitor/alerts/alerts-common-schema),
[log ingestion timing](https://learn.microsoft.com/en-us/azure/azure-monitor/logs/data-ingestion-time).

## 7. Optional SRE Agent and operator boundaries

The [current FAQ](https://learn.microsoft.com/en-us/azure/sre-agent/faq) lists
**Australia East, East US 2, Sweden Central**. The workload and agent may use
different regions; review data residency and cross-region access/charges. The
template exposes `sreAgentLocation` with those values. Model/provider availability
depends on the selected subscription/region; choose actual offered strings in
the setup wizard and use them as the explicit model parameters. The published
schema supports `defaultModel.name/provider`, not a fabricated consent object or
usage-limit property.

The opt-in module creates a UAMI for investigations and enables the agent's SAMI
for service infrastructure. It uses only documented `actionConfiguration`,
`defaultModel`, `knowledgeGraphConfiguration`, `logConfiguration`, and
`upgradeChannel` properties. `knowledgeGraphConfiguration.managedResources=[]`
is intentional: onboarding documents resource-group/subscription/management-group
selection, and this workshop RG contains production as well as demo. We do not
assume an undocumented slot-level picker or auto-connect the mixed group.

With the **additional** `grantSreInvestigationRoles=true` opt-in, only the UAMI
receives these scoped roles:

| Built-in role | Role ID | Scope |
|---|---|---|
| Reader | `acdd72a7-3385-48ef-bd42-f606fba81ae7` | Demo slot only |
| Monitoring Reader | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | Demo Application Insights only |
| Log Analytics Reader | `73c42c96-874c-492b-b04d-ab87d138a893` | Demo Log Analytics workspace only |

Role definitions have ancillary permissions (for example support operations);
review the current definitions and inherited roles. No subscription/RG-level
Contributor, Monitoring Contributor, production DB credential, user OBO token,
or automatic production remediation authority is granted. Merely knowing a
resource ID grants no permission.

**Complete and record these operator steps:**

1. Review the [creation/setup flow](https://learn.microsoft.com/en-us/azure/sre-agent/create-and-set-up),
   region, model/provider terms, data-residency notices, and cost consent before
   deployment. Record the approval ID, not tokens, in `sreAgentConsentReference`.
2. Assign an approved human **SRE Agent Administrator** at the agent scope via
   your privileged-access process; other users receive the least applicable agent
   user/reader role. ARM provisioning is not proof that a particular operator has
   portal/data-plane access. Do not grant the GitHub deployer broad tenant roles.
3. Use **Set up your agent → Code → Connect repositories** to connect only this
   repository using an approved GitHub App/OAuth method. Review scopes,
   organization approval, and connector health. Tokens/consent are not IaC.
   GitHub Enterprise Cloud may require a bring-your-own GitHub App.
4. Use **Logs** (or **Builder → Connectors**) to select only the demo Insights
   and workspace. Select the investigation UAMI when required. Review proposed
   portal role assignments first; do not expand them to production. Confirm a
   read-only query returns the known demo run and canonical metadata.
5. Do **not** blindly use “Add subscription/management group” or a broad
   “Privileged” resource-group preset. Onboarding documentation describes
   monitoring contributor/operator grants in some presets, even alongside Reader
   terminology. This template's explicit demo read scopes are deliberately
   narrower. Any additional API/connector permission is a reviewed manual change.
6. Configure the **Azure Monitor incident platform** in the current portal and
   bind the two exact demo rule IDs/action group according to that platform's
   supported connection flow. An email action group alone does not connect an
   agent. Do not invent a webhook URL, a secret-bearing receiver, or an ARM
   `incidentManagementConfiguration` payload to pretend this is provisioned.
7. Create/review an **incident response plan** that matches only these demo rules,
   `Environment=demo`, service, commit and run metadata. Configure investigation
   only; deny production writes and arbitrary log-provided tool instructions.
   Validate a known test alert and required evidence before enabling automation.
   Do not configure automatic escalation/OBO/privileged remediation.
8. In **Settings → Agent consumption**, set an approved monthly active-flow AAU
   allocation, record it, and set Cost Management alerts. Verify the selected model
   and read-only run mode. A provisioned ARM resource is not a configured loop.
9. Separately confirm the human GitHub account has the required Copilot coding
   agent entitlement, repository/org policy, permissions, and allowed model
   configuration. Human-approved handoff/PR/release gates remain in force.

Future remediation, if wanted, needs a separate change record and narrowly scoped
write identity/action, an authorized approver, and an audited execution path.
Do not convert the read-only investigator to broad production Contributor.

Sources: [overview](https://learn.microsoft.com/en-us/azure/sre-agent/overview),
[agent identity](https://learn.microsoft.com/en-us/azure/sre-agent/agent-identity),
[permissions/OBO](https://learn.microsoft.com/en-us/azure/sre-agent/permissions),
[connectors](https://learn.microsoft.com/en-us/azure/sre-agent/connectors),
[incident platforms](https://learn.microsoft.com/en-us/azure/sre-agent/incident-platforms),
[IaC boundary](https://learn.microsoft.com/en-us/azure/sre-agent/deploy-iac),
[Reader definition](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/general#reader),
[monitoring role definitions](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/monitor).

## 8. GitHub OIDC and approval prerequisites

Create separate Entra application/service-principal deployment identities for
`staging`, `production`, and `demo`. Use issuer
`https://token.actions.githubusercontent.com`, audience `api://AzureADTokenExchange`,
and the repository's **actual environment-scoped subject**.

Repositories created, renamed, or transferred after **July 15, 2026** use immutable
default subjects with owner/repository IDs; older repositories may opt in.
**Do not infer the active configuration from a date or copy the legacy string.**
Read nonsecret repository metadata and the OIDC customization settings:

```bash
gh api repos/<owner>/<repository> > infrastructure/operator.repository.json
gh api repos/<owner>/<repository>/actions/oidc/customization/sub \
  > infrastructure/operator.oidc.json
python -m scripts.oidc_subject \
  --repository-json infrastructure/operator.repository.json \
  --settings-json infrastructure/operator.oidc.json --environment staging \
  > infrastructure/operator.staging-federation.json
```

Repeat generation for production/demo. Review `use_default`,
`use_immutable_subject`, `sub_claim_prefix`, and the owner/repository IDs. The
generator supports default subjects and explicit `repo,context` templates;
missing immutable settings, inconsistent prefixes, and other custom/inherited
templates block for separate review, not a guessed fallback. Expected shapes:

```text
repo:<owner>@<owner-id>/<repository>@<repository-id>:environment:<environment>
repo:<owner>/<repository>:environment:<environment>  # only when actually configured
```

The generated JSON is federation **metadata**, not a credential or identity write.
An authorized identity administrator separately creates/reviews that exact Entra
federated credential. Put its subject in `expected_oidc_subject` in the nonsecret
preflight configuration. `--live github` compares current GitHub settings;
`--live azure` reads the Entra application's federated credentials and checks
subject/issuer/audience (requires directory read permission). Neither requests,
decodes, or logs a raw OIDC JWT. Recheck after renames/transfers/template changes.

Jobs must actually declare the matching GitHub environment before receiving
`id-token: write`. Do not also authorize an unrestricted branch subject for the
production principal. Keep workflow permissions otherwise at `contents: read`
unless a specific handoff job needs a narrower additional permission.

The infrastructure bootstrap identity and application-delivery identity are
different concerns. An authorized infrastructure operator needs only the selected
RG resource-provider deployment actions, Key Vault ARM-secret retrieval, and (if
explicitly opted into the three reader assignments) role-assignment authority at
those exact resource scopes. Application delivery does not need Owner, User
Access Administrator, or subscription Contributor.

Constrain a delivery principal to the relevant app/slot operations. Staging/demo
publish identities should not have production mutation/swap access. The protected
production job needs reviewed production deployment/swap/config-read actions at
the app scope; do not give it access to unrelated resource groups. Custom roles
must be reviewed against the selected Azure deployment action's actual API calls.
Built-in Website Contributor at an **app** scope is broader than upload-only;
do not describe it as a no-write investigator role. Never automatically assign
broad Contributor merely to make deployment succeed.

Environment settings:

- OIDC identifiers: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
  `AZURE_SUBSCRIPTION_ID`.
- App/group identifiers: `AZURE_WEBAPP_NAME`, `AZURE_RESOURCE_GROUP`.
- Environment-scoped protected `MIGRATION_DATABASE_URL` and nonsecret
  `DATABASE_NAME=<exact-logical-database>`,
  `MIGRATOR_USER=task_<env>_migrator`,
  `RUNTIME_USER=task_<env>_runtime`. TLS verification is required in jobs.
- Trusted private runner group/label; align these with the workflow's
  `runs-on`/group inputs. The preflight config records `runner_group` and
  `runner_label`; it does not provision or select a runner.
- Nonsecret preflight JSON file path `PREFLIGHT_CONFIG`, when preflight is used.
  Keep values in the configuration aligned with the exact protected job.
- Repository-level `AZURE_RUNNER_LABELS` JSON containing `self-hosted`, `linux`,
  `x64`, and a private-runner label; `EPHEMERAL_RUNNER_CONFIRMED=true`,
  `ADMIN_BYPASS_DISABLED_CONFIRMED=true`, and
  `SWAP_KEY_VAULT_READY_CONFIRMED=true` only after the documented review.
  These attestations do not pretend to be live policy/API proof.

Configure production required reviewers, prevent self-review, disable admin
bypass where available, and allow only the protected release branch (`main`).
Use **selected branches**, exactly `main` (not wildcards/tags), for staging,
production, and demo. Demo requires reviewers and prevention of self-review too:
dispatch its trusted workflow from `main`; the separately resolved candidate is
`demo/<run-id>`, not the workflow ref. The REST environment schema does not expose
administrator-bypass state, so confirm that in the UI and record the attestation.
Protect `main` with required passing CI, at least one approving review,
dismissal of stale approvals, admin enforcement, no force-pushes/deletions, and
no unreviewed bypass actors. Review repository/organization rulesets as well.
Environment/ruleset feature availability depends on the GitHub plan/repository.
A missing API permission or unsupported protection feature is a blocker, not a
reason to quietly skip the gate. Demo activation should also be approval-gated.

Sources: [Azure OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-azure),
[OIDC customization REST](https://docs.github.com/en/rest/actions/oidc),
[environment protections](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).

## 9. Offline-first preflight and optional read-only checks

Copy `infrastructure/preflight.example.json` to an untracked, nonsecret file in
the checkout, fill the exact environment/subscription/resource identifiers, and
replace each empty attestation with a reviewed change/evidence reference.
The example intentionally fails. No credential, connection string, OAuth token,
or private incident contents belong in this JSON.

```bash
# Configuration-only: no subprocess, Azure, GitHub, HTTP, DB, or DNS calls.
python -m scripts.preflight --config infrastructure/operator.preflight.json

# A protected migration job can additionally validate its own environment locally.
python -m scripts.preflight --config infrastructure/operator.preflight.json \
  --sha <packaged-commit-sha> \
  --require-env AZURE_CLIENT_ID --require-env AZURE_TENANT_ID \
  --require-env AZURE_SUBSCRIPTION_ID --require-env MIGRATION_DATABASE_URL \
  --require-env DATABASE_SSL_REQUIRED
```

Output is structured JSON on stdout, exit code `0` for the requested checks passing
or `1` for `blocked`. Default status **`configuration_ready` is not cloud-verified**.
Attestations are explicitly `evidence_kind=operator_attestation`,
`status=attested`, never `live_api`. Missing/invalid configuration or prerequisite
attestations block before any optional live calls. Values/CLI stderr/HTTP bodies
are not echoed. `PREFLIGHT_CONFIG` is a file path, not inline JSON.

`run_preflight(configuration, live=(), required_environment=(), environ=...)` is
also importable by delivery tooling. It returns the same JSON-serializable report
and defaults to no I/O. Use `--sha` to bind configuration to the exact packaged
commit, not to a mutable branch name.

Only after operator authorization, explicitly select checks (repeat `--live`):

| Selection | Read-only evidence, and limits |
|---|---|
| `--live azure` | Azure CLI login tenant/subscription, Entra federation issuer/audience/subject, registered provider region/type (and pinned SRE API when applicable), resource GET access, private DB network settings, per-env workspace linkage, slot identity, sticky database/settings, and matching Key Vault **reference**/telemetry destination. Does not fetch secret values or prove effective RBAC/Key Vault resolution. `appsettings list` can contain sensitive values in memory; they are never output. |
| `--live github` | GitHub user/repo permission, actual immutable/default OIDC settings, production and selected environment reviewers/self-review/branch policies, and branch protection CI/review/admin settings. Uses REST GETs only. Does not prove Copilot entitlement, effective org ruleset bypass, or an entire repository's policy. Rulesets-only protection that cannot be established by these endpoints must be reviewed separately; selected checks fail closed on 403/404. |
| `--live copilot` | With separately supplied `COPILOT_USER_TOKEN`, reads the user identity and GraphQL assignable-actor eligibility. No assignment, mutation, task launch, or AI-credit consumption is requested. This proves current user/repository eligibility, not acceptance of a future task. |
| `--live readiness` | GET `/live` and `/ready`, exact environment, schema revision, packaged commit and scenario state. Schema/DB status is observed through the app, not proof of migration privileges. No CRUD/traffic injection. |
| `--live telemetry` | A read-only Log Analytics `POST /query`, last 15 minutes, matching service/environment/commit/correlation and run ID when configured. No matching data is a failure. This does not prove alert delivery. |
| `--live sre` | Published ARM GET: provisioned agent, separate identities, selected model/region, `ReadOnly`/`Low` mode, and no broad connected RG scopes. Does not prove connector health, OAuth consent, user roles, effective inherited RBAC, response-plan filters, or usage ceilings. |

The appsetting-read check requires `Microsoft.Web/sites/config/list/action`
(or the slot equivalent), not just Reader. Run it only as an approved
operator/delivery identity; do not grant the investigator config-secret access
merely to run preflight.

For SRE checks set `sre_agent` in the **demo** preflight config to an object with
`name`, `location`, `model_name`, `model_provider`, `consent_reference`. Add evidence
references for `sre_provider_consent`, `sre_code_and_log_connectors`,
`sre_incident_response_plan`, `sre_user_roles`, `sre_readonly_effective_rbac`,
`sre_active_usage_limit`. These remain attestations even after the ARM probe passes.
An enabled demo config also requires a nonempty 1–48-character lowercase
`demo_run_id`; production/staging cannot declare a run or enable the scenario.

Example **operator-run**, read-only invocation:

```bash
python -m scripts.preflight --config infrastructure/operator.preflight.json \
  --live azure --live github --live readiness --live telemetry
```

No helper automatically logs in, registers a provider, installs CLI extensions,
assigns roles, changes GitHub protections, connects an SRE connector, runs
migrations, provisions resources, or performs an incident test. All live calls
in the unit tests are mocked. No live Azure end-to-end result is claimed.

## 10. Costs, bounded usage, and explicit teardown

Do not use a fixed monthly demo estimate. Before opting in, record **quote date,
currency, region, SKU, contracted rate, and expected usage** in the approved
cost record, using the [Azure calculator](https://azure.microsoft.com/en-us/pricing/calculator/)
and service price pages. The template's example region is **East US 2**; the
pricing documentation was reviewed **2026-09-20**, not a binding regional quote.

- S1 compute accrues for the **plan** even if an app/slot is stopped. Slots share
  plan capacity; stopping a site is not a plan-cost savings guarantee.
- PostgreSQL compute, storage/backups, networking, Application Insights/Log
  Analytics ingestion/retention/query and alert rules, and notification usage
  may be billable. Stopping PostgreSQL is not a permanent zero-cost state;
  storage/backups persist and service stop-duration limits apply.
- [SRE Agent billing](https://learn.microsoft.com/en-us/azure/sre-agent/pricing-billing)
  currently documents **4 AAUs per agent-hour always-on**, continuing while the
  resource exists, **plus active-flow AAUs** metered by model input/output/cache
  tokens. Consult the [regional price page](https://azure.microsoft.com/en-us/pricing/details/sre-agent/)
  for currency-per-AAU; no invented dollar total is supplied here.
- Stopping an agent stops active processing, **not its always-on charge**. Deleting
  the agent stops that agent's baseline. Trial eligibility/waivers are conditional,
  not a free default assumed by this repository.
- Set a reviewed active-flow allocation in the current portal; the reviewed docs
  list 500–1,000,000 AAUs. Hitting an active-flow limit does not stop always-on
  billing. The current ARM schema used here does not expose that allocation, so
  no fabricated Bicep field is used.
- Set Cost Management budgets/notifications, log retention/ingestion policies,
  runner lifetime/job timeouts, scenario request/duration bounds, and narrow
  incident filters. Budgets are **notifications, not hard spend caps**. Disabling
  ingestion can also hide incidents; make that tradeoff explicit.

When the whole workshop is disposable, inspect the exact group:

```bash
# Offline plan: no cloud calls.
bash infrastructure/teardown.sh \
  --resource-group rg-sre-demo-<unique> --subscription <subscription-uuid>

# Explicit READ-ONLY inventory with name and ownership/disposable-tag checks.
bash infrastructure/teardown.sh \
  --resource-group rg-sre-demo-<unique> --subscription <subscription-uuid> \
  --list --confirm-resource-group rg-sre-demo-<unique>
```

Only after reviewing backups/export/retention and **every listed resource**, an
operator may replace `--list` with `--delete` and add
`--confirm-subscription <exact-subscription-uuid>`. The helper again lists resources
and requires `purpose`, `deployment`, and `disposable=true` group tags. It refuses
an unowned/shared resource and refuses empty/default/shared group names. It never
purges vaults, deletes another group, or performs implicit cleanup during deploy.

External Key Vaults/secrets, network links, federated credentials/role assignments
at outside scopes, GitHub settings, retained monitoring data and exported
artifacts are **not** implicitly removed. Review them separately, verify actual
deletion/billing state and retention obligations, and remove only explicitly
approved workshop-owned items. No teardown command was executed for this PR.
