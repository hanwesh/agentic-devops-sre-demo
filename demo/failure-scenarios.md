# Failure scenarios and step-by-step remediation

Use this playbook to demonstrate **detect -> investigate -> approve -> remediate ->
verify -> record -> reset**. These are operator-run exercises, not instructions for
an agent to change Azure automatically. No live failure or recovery in this document
has been executed or verified by its author.

The repository's automated issue/Copilot contract supports the **bad-code scenario
S3 only**. The other scenarios are documented, approval-gated operational runbooks.
They must not be disguised as successful automated Copilot remediation.

## Scenario catalogue

| ID | Failure and safe injection | Expected evidence | Recovery owner | Exercise ceiling |
|---|---|---|---|---|
| [S1](#s1-app-service-is-down) | Stop **only the demo App Service slot** | External probe fails; slot state `Stopped`; application telemetry may disappear | App operator starts that slot | Restore within 5 minutes |
| [S2](#s2-database-is-unavailable) | Revoke the demo runtime role's connection privilege and terminate only its demo DB sessions | `/live` remains available; readiness/task requests fail; connection/permission errors | DBA restores that exact privilege | Restore within 5 minutes |
| [S3](#s3-bad-code-causes-http-500) | Commit the known missing-status regression to `demo/<run-id>` | `/ready` healthy; original filter endpoint produces `KeyError`/500; positive tests fail | Copilot or developer proposes code; human reviews/deploys | 30-minute session; abort/reset if blocked |
| [S4](#s4-slow-requests-from-database-lock-contention) | Hold a bounded lock on the **demo** tasks table | Slow task requests, possible readiness timeout, PostgreSQL lock wait | DBA releases the transaction; developer investigates recurrence | 20-second normal hold; 25-second statement / 30-second idle-transaction timeouts |
| [S5](#s5-schema-object-is-missing) | Rename the demo tasks table temporarily, preserving its data | Liveness works; readiness fails; missing-relation exception despite a head revision | Migrator restores the original table name | Restore within 5 minutes |
| [S6](#s6-configuration-drift-blocks-readiness) | Change **only the demo slot's** `DATABASE_NAME` setting | `/ready` 503 with `database_identity_mismatch`; actual DB may still work | App operator restores the approved setting | Restore within 5 minutes |

The ceilings are workshop stop conditions, **not measured RTOs or SLAs**. Assign a
named recovery operator and a second terminal before injection. No intentional data
loss is part of these exercises. If the expected scope, rollback command, or
permissions cannot be confirmed, do not inject.

**Important:** the delivered PostgreSQL server and App Service plan are shared by
the workshop's production, staging and demo environments. Never stop that server,
change its firewall, saturate its CPU, or scale down the shared plan for a demo-only
exercise. S2 simulates database unavailability at the logical-database/user boundary;
it is **not** a claim that the PostgreSQL server process went down.

## What will actually detect these failures?

| Signal | Delivered today | Limits / operator addition |
|---|---|---|
| `/live` | Process liveness and deployment identity | Not a database check; a stopped slot can return a platform error rather than application JSON |
| `/ready`, `/health` | Schema/DB/build identity checks; unhealthy contract is HTTP 503 | Inspect the body, not just HTTP 200. An unexpected 500 is a readiness-contract defect, not a pass |
| Demo log alerts | Five-minute weighted 5xx ratio **>5%**, or weighted p95 **>3000ms**, with **at least 20 observed nonprobe records** | Opt-in and dependent on real telemetry ingestion. Health probes are excluded |
| Stopped app / no telemetry | Manual external probe and Azure resource/activity inspection | Error-ratio alerts cannot detect zero requests. There is no provisioned synthetic-availability or missing-telemetry alert |
| Operational paging | SRE connector/incident platform requires operator configuration | An operator may add an approved external availability test/alert; its cost, interval, failure threshold and routing need separate review |

For a workshop, three consecutive failed probes 30 seconds apart can be an
**operator observation policy**, not a claim that such an alert is deployed. Do not
wait for all three if the affected resource is unexpectedly production: abort and
escalate immediately. Do not increase load or lower thresholds to force an alert.

## 1. Preparation shared by every scenario

1. Review the [Azure setup](../docs/azure-setup.md) and
   [SRE setup](../docs/sre-agent-setup.md). The reliability baseline and trusted
   workflow must first be human-reviewed and available on the default branch.
   Establish a healthy, explicitly enabled **demo** deployment through the
   [presenter procedure](README.md). Use only synthetic workshop data.
2. Reserve an exclusive exercise window. No simultaneous deployment, migration,
   swap, second presenter or shared-server maintenance. Record the approver,
   recovery operator, target resource IDs and stop time.
3. For **S1, S2, S4, S5 and S6**, an authorized repository operator sets
   `SRE_HANDOFF_ENABLED=false` and pauses the code-regression issue-publishing
   response plan before injection. Record the previous setting/plan state.
   Existing in-flight agent work is not cancelled by changing this variable.
   Keep read-only SRE investigation available, with remediation requiring approval.
   Use a normal manual exercise issue, not the trusted `sre-incident` envelope.
4. Prepare a nonsecret demo preflight configuration. Run the offline check first;
   live read-only checks require separate operator authorization:

   ```bash
   python -m scripts.preflight --config infrastructure/operator.demo.json
   # Only after approval; these probes do not inject a fault:
   python -m scripts.preflight --config infrastructure/operator.demo.json \
     --live azure --live github --live readiness --live telemetry
   ```

5. In an operator Bash terminal, fill these values from the **approved inventory**,
   not incident prose. `RUN_ID` is the already deployed application's run ID;
   `EXERCISE_ID` is a fresh evidence identifier for this particular operational
   exercise. Preserve both when diagnosing it. For S3, follow its separate new-run
   branch procedure instead.

   ```bash
   set -euo pipefail
   export AZURE_SUBSCRIPTION_ID="<approved-subscription-uuid>"
   export AZURE_RESOURCE_GROUP="rg-sre-demo-<approved-name>"
   export AZURE_WEBAPP_NAME="<approved-app-name>"
   export DEMO_DATABASE="<approved-demo-logical-database>"
   export PRODUCTION_DATABASE="<approved-production-logical-database>"
   export STAGING_DATABASE="<approved-staging-logical-database>"
   export DEMO_RUNTIME_USER="task_demo_runtime"
   export DEMO_MIGRATOR_USER="task_demo_migrator"
   export RUN_ID="<currently-deployed-demo-run-id>"
   export EXPECTED_SHA="<approved-healthy-40-character-deployed-sha>"
   export EXERCISE_ID="s1-20260921-attempt1"
   export DEMO_URL="https://${AZURE_WEBAPP_NAME}-demo.azurewebsites.net"

   [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]
   [[ "$EXERCISE_ID" =~ ^s[1-6]-[a-z0-9-]+$ ]]
   [[ "$DEMO_DATABASE" =~ ^[a-z][a-z0-9_]{0,62}$ ]]
   [[ -n "$DEMO_DATABASE" && -n "$PRODUCTION_DATABASE" && -n "$STAGING_DATABASE" ]]
   [[ "$DEMO_DATABASE" != "$PRODUCTION_DATABASE" ]]
   [[ "$DEMO_DATABASE" != "$STAGING_DATABASE" ]]
   [[ "$DEMO_DATABASE" != postgres && "$DEMO_DATABASE" != template0 ]]
   [[ "$DEMO_DATABASE" != template1 ]]
   python infrastructure/safety.py resource-group "$AZURE_RESOURCE_GROUP"

   demo_webapp() {
     az webapp "$@" --subscription "$AZURE_SUBSCRIPTION_ID" \
       --resource-group "$AZURE_RESOURCE_GROUP" --name "$AZURE_WEBAPP_NAME" \
       --slot demo --only-show-errors
   }
   DEMO_RESOURCE_ID="$(demo_webapp show --query id --output tsv)"
   [[ "$DEMO_RESOURCE_ID" == */slots/demo ]]
   demo_webapp show --query '{id:id,state:state,host:defaultHostName}' --output json
   mkdir -p demo/evidence
   mkdir "demo/evidence/$EXERCISE_ID"
   ```

   Confirm the returned subscription/resource ID and hostname match the approved
   inventory and URL. The existing HTTP tools deliberately reject unrecognized
   hostnames; do not bypass their target checks. A new shell needs the reviewed
   variables/functions again. Keep credentials out of commands, files and logs.
6. Save a successful baseline and inspect the normal task endpoint:

   ```bash
   python -m scripts.smoke --url "$DEMO_URL" --environment demo \
     --sha "$EXPECTED_SHA" --demo-run-id "$RUN_ID" \
     --database "$DEMO_DATABASE" --crud \
     --output "demo/evidence/$EXERCISE_ID/baseline.json"
   ```

   This creates and deletes only its marker-owned sample. Do not inject if baseline
   readiness, schema, identity, CRUD or cleanup fails.
7. For SQL scenarios, use a private-network operator host with `psql` 15+, verified
   server/DNS identity, TLS and protected credentials. Set `PGHOST` to the approved
   server FQDN. Supply `PGUSER`/authentication through the approved credential
   mechanism; never paste a password or URL into the command. Define:

   ```bash
   demo_psql() {
     : "${PGHOST:?Approved private PostgreSQL FQDN required}"
     : "${PGUSER:?Approved DBA or demo migrator login required}"
     PGCONNECT_TIMEOUT=5 PGSSLMODE=verify-full \
       psql -X --no-password --set=ON_ERROR_STOP=1 \
       --host="$PGHOST" --username="$PGUSER" --dbname="$DEMO_DATABASE" "$@"
   }
   demo_psql -c 'SELECT current_database(), current_user;'
   ```

   The DBA account used for S2 needs database ACL administration and permission to
   terminate the selected backends. S4/S5 use the **demo migrator**, which owns its
   tables. Runtime credentials must never gain DDL or administrator permissions.

## 2. Common investigation and recovery checks

Before remediation, record UTC times, original symptom/HTTP status, `EXPECTED_SHA`,
`RUN_ID`, `EXERCISE_ID`, resource ID, and correlation/trace IDs when actually present.
Do not invent trace IDs for Azure-generated error pages or absent telemetry.

These probes are bounded. A timeout, HTML error page or missing JSON is evidence,
not a successful health check. Task response bodies may contain data; discard them.

```bash
curl --silent --show-error --connect-timeout 3 --max-time 10 \
  --write-out '\nHTTP %{http_code}\n' "$DEMO_URL/live"
curl --silent --show-error --connect-timeout 3 --max-time 10 \
  --write-out '\nHTTP %{http_code}\n' "$DEMO_URL/ready"
curl --silent --show-error --connect-timeout 3 --max-time 10 \
  --output /dev/null --write-out 'HTTP %{http_code}; seconds %{time_total}\n' \
  "$DEMO_URL/api/tasks"
```

Run probes individually when an expected transport failure would stop a
`set -e` shell. Preserve the actual exit status; do not append `|| true` and call
the result healthy. If permissions/network access are missing, escalate rather
than weakening a firewall, role, test or alert.

| Observation | First investigation |
|---|---|
| `/live` unreachable or platform error | Slot state, Resource Health, Activity Log, startup/deployment logs |
| `/live` 200, `/ready` not ready | Readiness reason, DB connectivity/permissions, schema and runtime identity |
| `/ready` 200, one endpoint 500 | Exact deployed commit, traceback, endpoint-specific regression |
| Slow requests / intermittent readiness timeout | Lock waits, query duration, connection pool, server metrics |
| `database_identity_mismatch` | Sticky expected DB setting versus actual DB; do not redirect the app blindly |

Use the demo workspace and actual observation interval in
[the telemetry runbook](../docs/incident-response.md#observe-before-publishing).
Scope `AppRequests`/`AppExceptions` by component, environment, run ID and commit.
Empty `AppRequests` during an outage is not evidence of a 0% failure rate.
Keep raw SQL, credentials, request bodies and full stack traces in controlled logs,
not public issues.

After each approved repair, run the same smoke command with a **new**
`--output "demo/evidence/$EXERCISE_ID/recovery.json"` filename and the expected
recovered commit. Require correct JSON, schema, database, run and commit identities,
normal endpoint behavior and verified sample cleanup. Also recheck the **original
failed operation** specified below. Never resolve an incident solely because an
Azure start/update command returned success.

## S1. App Service is down

**Scope/approval:** demo slot only; App Service stop/start permission at that
scope. This does not stop the paid plan or simulate an Azure-wide regional outage.

1. **Inject, after preparation:** record the start time and stop only that slot:

   ```bash
   demo_webapp stop --output none
   demo_webapp show --query state --output tsv
   ```

2. **Detect:** run the bounded `/live` and `/ready` probes. Expect non-success,
   often a platform-generated 403/503 page, not necessarily application JSON.
   Capture observed status and the actual `Stopped` state. Request-ratio alerts
   may never fire because the app cannot emit requests.
3. **Diagnose:** correlate the slot-scoped Activity Log stop event with its actor
   and time. Check Resource Health before attributing an outage to a code deploy.
   If Azure says `Running` but the process is unavailable, inspect startup logs,
   the deployed artifact and configuration instead of assuming a stop action.
4. **Remediate:** the operator approves and runs:

   ```bash
   demo_webapp start --output none
   ```

   For a real startup regression, restore the reviewed demo artifact through the
   protected demo workflow; do not swap it into production or randomly restart
   until a code/configuration failure appears resolved.
5. **Verify/reset:** require `Running`, successful `/live`, schema-aware readiness,
   normal CRUD and the same expected commit/run. Check fresh telemetry after
   ingestion. Keep the slot running, record recovery duration and close only
   after the shared completion checklist.

## S2. Database is unavailable

**Scope/approval:** only `DEMO_DATABASE` and `task_demo_runtime`; DBA-approved ACL
change and termination of those sessions. Staging/production and their roles must
remain reachable. This simulates the application's loss of database access.

1. **Prepare:** confirm the DBA terminal targets the demo DB. Record the existing
   effective privilege; it must be `true` before this drill:

   ```bash
   demo_psql <<'SQL'
   \getenv runtime_role DEMO_RUNTIME_USER
   SELECT current_database(),
          has_database_privilege(:'runtime_role', current_database(), 'CONNECT');
   SQL
   ```

2. **Inject:** revoke new connections, then end only already-pooled connections
   for that same DB/user. Review the selected session IDs before the second call.

   ```bash
   demo_psql <<'SQL'
   \getenv runtime_role DEMO_RUNTIME_USER
   \getenv demo_database DEMO_DATABASE
   REVOKE CONNECT ON DATABASE :"demo_database" FROM :"runtime_role";
   SELECT pid, datname, usename FROM pg_stat_activity
   WHERE datname = current_database() AND usename = :'runtime_role'
     AND pid <> pg_backend_pid();
   SQL
   ```

   ```bash
   demo_psql <<'SQL'
   \getenv runtime_role DEMO_RUNTIME_USER
   SELECT pid, pg_terminate_backend(pid) AS terminated FROM pg_stat_activity
   WHERE datname = current_database() AND usename = :'runtime_role'
     AND pid <> pg_backend_pid();
   SQL
   ```

   Existing connections survive `REVOKE CONNECT`; omitting termination can make
   the exercise appear ineffective. If inherited/PUBLIC privileges still allow
   connections, restore the baseline and investigate grants; do not revoke
   unrelated/shared roles. The bootstrap normally removes PUBLIC access.
3. **Detect/diagnose:** `/live` should remain 200 while readiness and task requests
   fail. Readiness should be 503; if a driver authentication error instead reaches
   the generic 500 handler, record a separate readiness-contract defect. Either
   result is unhealthy. Correlate the actual connection/permission error with the
   ACL change. Confirm the server and other environment databases are still up.
4. **Remediate:** restore only the privilege this drill removed:

   ```bash
   demo_psql <<'SQL'
   \getenv runtime_role DEMO_RUNTIME_USER
   \getenv demo_database DEMO_DATABASE
   GRANT CONNECT ON DATABASE :"demo_database" TO :"runtime_role";
   SQL
   ```

   Existing invalid pooled connections may need to be discarded; allow bounded
   readiness retries. If recovery still fails, inspect DNS/TLS/credentials and
   application errors before approving a **demo-only** restart.
5. **Verify/reset:** require effective CONNECT, normal readiness/CRUD and the
   original task read. No schema/data restoration should be necessary. Verify
   no staging/production grant changed and close the privileged DBA session.

### If the PostgreSQL server really is down

Do not run a Flexible Server stop drill on the delivered shared server. A genuine
stop/start rehearsal requires a **separately approved disposable server** with no
staging/production clients, its own cost/backup/recovery plan, and independently
verified target IDs. Provisioning that topology is not part of this playbook.

For an actual **whole-server** outage, use this distinct remediation path:

1. Declare its real blast radius and incident severity; identify every dependent
   environment. Obtain the appropriate production/DBA approval, not merely the
   demo-slot approval. Set `INCIDENT_DB_SERVER` and `INCIDENT_DB_RG` from the
   verified resource inventory, never from untrusted incident commands.
2. Read the server state and inspect Resource Health, Activity Log, private
   DNS/network reachability and maintenance events:

   ```bash
   az postgres flexible-server show --subscription "$AZURE_SUBSCRIPTION_ID" \
     --resource-group "$INCIDENT_DB_RG" --name "$INCIDENT_DB_SERVER" \
     --query '{id:id,state:state,host:fullyQualifiedDomainName}' --output json
   ```

3. If it is `Stopped` and a start is explicitly approved, the authorized operator
   restores the **whole server**, not just one database:

   ```bash
   az postgres flexible-server start --subscription "$AZURE_SUBSCRIPTION_ID" \
     --resource-group "$INCIDENT_DB_RG" --name "$INCIDENT_DB_SERVER" \
     --only-show-errors --output none
   ```

4. If the server is `Ready` but the app cannot connect, diagnose DNS, routes,
   TLS and authentication instead. Do not restart repeatedly, open the firewall
   to all Azure addresses, or change unrelated role grants. Escalate a platform
   outage to Azure support according to the operator's service plan.
5. Verify readiness and original reads for **every affected environment**, with
   production checks read-only. Reconcile errors/telemetry and record actual
   recovery before resolving the incident. Do not restore a backup over an
   existing server/database to conceal an access fault.

## S3. Bad code causes HTTP 500

**Scope/approval:** a new disposable `demo/<run-id>` branch and the protected demo
slot. This is the only scenario wired to the strict incident/Copilot handoff.

1. **Prepare a new run:** set `RUN_ID` to a new slug (not the previous exercise's
   deployed run ID) and `EXPECTED_SHA` to a reviewed healthy full baseline
   SHA in a clean operator checkout. Complete the
   [handoff configuration](../docs/sre-agent-setup.md#4-configure-the-supported-copilot-handoff)
   before enabling its response plan and `SRE_HANDOFF_ENABLED`. Never enable a paid
   assignment just to test configuration; use the read-only eligibility preflight.
2. **Introduce and commit the regression:**

   ```bash
   python -m scripts.demo_branch introduce \
     --run-id "$RUN_ID" --base "$EXPECTED_SHA" \
     --environment demo --acknowledge-disposable-demo
   ```

   The tool creates `demo/<run-id>` and a commit replacing the optional status
   fallback with a failing lookup. Inspect the diff. Preserve both positive
   regression tests; they must now fail. Record the emitted regression SHA.
3. **Publish and deploy after approval:**

   ```bash
   git push --set-upstream origin "demo/$RUN_ID"
   gh workflow run demo.yml --ref main \
     -f branch="demo/$RUN_ID" -f run_id="$RUN_ID" \
     -f scenario_state=regression -f issue_number=0
   ```

   `main` is the trusted workflow ref; the input names the separately pinned demo
   candidate. The demo environment reviewer approves deployment and its bounded
   fault exercise. Do not use this exception for normal CI or production.
4. **Detect/diagnose:** the workflow's GET-only driver verifies normal controls
   and the failing `GET /api/tasks?filter=broken`. Expect a healthy `/ready`,
   actual `KeyError`/500, exact regression commit and the same run ID. Confirm a
   real alert/telemetry window; a driver report alone does not prove alert delivery.
   Follow the [incident runbook](../docs/incident-response.md) to publish/update one
   validated issue. Record actual Copilot acceptance or an explicit manual fallback.
5. **Remediate:** Copilot/developer fixes the missing fallback and adds meaningful
   regression coverage. Keep the scenario enabled, original tests intact and
   alert thresholds unchanged. Human-review the PR against `demo/<run-id>`,
   approve protected workflows where required, and merge only after full CI passes.
6. **Deploy and verify the fix:**

   ```bash
   gh workflow run demo.yml --ref main \
     -f branch="demo/$RUN_ID" -f run_id="$RUN_ID" \
     -f scenario_state=healthy -f issue_number="$INCIDENT_ISSUE_NUMBER"
   ```

   Require an approved healthy deployment and the **original URL returning valid
   200 task-list JSON** at the fix SHA with the scenario still enabled. Record the
   PR, CI, deployment, endpoint check and fresh telemetry/alert resolution links.
7. **Abort/reset:** if the exercise cannot complete, use:

   ```bash
   python -m scripts.demo_branch reset \
     --run-id "$RUN_ID" --environment demo --acknowledge-disposable-demo
   ```

   Then review/publish/deploy that forward fix.
   This is not a history reset or an Azure deployment. Keep old issue/assignment
   reservations; the next exercise gets a new run ID, never a duplicate paid task.

## S4. Slow requests from database lock contention

**Scope/approval:** only the demo tasks table. No CPU stress test, connection flood
or shared-server resizing. Use the demo migrator and a second operator terminal.

1. **Prepare:** confirm S3 handoff is paused and the baseline is healthy. Set
   `PGUSER`/authentication to the approved demo migrator and give this session an
   identifiable application name:

   ```bash
   export PGAPPNAME="sre-drill-$EXERCISE_ID"
   ```

2. **Inject in terminal A:** the transaction holds a lock for 20 seconds, then
   rolls back. A lock-acquisition failure aborts instead of waiting indefinitely.

   ```bash
   demo_psql <<'SQL'
   BEGIN;
   SET LOCAL lock_timeout = '3s';
   SET LOCAL statement_timeout = '25s';
   SET LOCAL idle_in_transaction_session_timeout = '30s';
   SELECT pg_backend_pid() AS drill_backend;
   LOCK TABLE public.tasks IN ACCESS EXCLUSIVE MODE;
   SELECT pg_sleep(20);
   ROLLBACK;
   SQL
   ```

   The idle-transaction timeout also releases locks if the client loses its
   connection without completing `ROLLBACK`; the normal 20-second hold is not
   a guarantee for a lost client. Escalate if the lock remains after 60 seconds.

3. **Observe in terminal B:** while the lock is held, issue **one** bounded task
   read (30-second client deadline) and independently inspect waiters:

   ```bash
   curl --silent --show-error --connect-timeout 3 --max-time 30 \
     --output /dev/null --write-out 'HTTP %{http_code}; seconds %{time_total}\n' \
     "$DEMO_URL/api/tasks"
   ```

   ```bash
   demo_psql -c "SELECT pid, application_name, wait_event_type, wait_event,
     pg_blocking_pids(pid) AS blockers
     FROM pg_stat_activity WHERE datname = current_database();"
   ```

   A readiness probe can time out while its task-column check waits on the same
   lock. Do not use `demo.traffic --mode fault`: it expects the S3 `KeyError` and
   would correctly reject this different fault. One request does **not** meet the
   deployed 20-record alert floor; demonstrate timing/wait evidence, not a fictional
   p95 alert. A larger load exercise needs separate approval and bounded tooling.
4. **Remediate:** allow the known transaction to roll back. If operator/session
   failure leaves a blocker, verify its captured PID, demo DB/user and
   `PGAPPNAME` together before a DBA approves terminating that one backend.
   Never kill every PostgreSQL session. For recurring real latency, investigate
   transaction lifetime, query plans/indexes and pool usage before proposing a
   tested code/migration PR.
5. **Verify/reset:** confirm the holder/waiters disappear, the original task read
   returns normally, readiness/CRUD pass and telemetry records actual latency.
   Do not claim an unmeasured p95 improvement from a single fast response.
   Remove the temporary `PGAPPNAME` from the operator shell.

## S5. Schema object is missing

**Scope/approval:** reversible table-name damage in the demo DB, using its migrator.
This tests schema-aware readiness, not a destructive migration downgrade.

1. **Prepare:** verify `public.tasks` exists, `public.tasks_drill_backup` does not,
   and there are no unrelated schema operations. Stop if a previous drill's backup
   exists; do not overwrite or delete it.

   ```bash
   demo_psql -c "SELECT current_database(), current_user,
     to_regclass('public.tasks') AS tasks,
     to_regclass('public.tasks_drill_backup') AS drill_backup;"
   ```

2. **Inject after approval:**

   ```bash
   demo_psql <<'SQL'
   BEGIN;
   SET LOCAL lock_timeout = '3s';
   SET LOCAL statement_timeout = '10s';
   SET LOCAL idle_in_transaction_session_timeout = '30s';
   ALTER TABLE public.tasks RENAME TO tasks_drill_backup;
   COMMIT;
   SQL
   ```

3. **Detect/diagnose:** `/live` stays available; `/ready` becomes 503 and task reads
   fail. The readiness reason is `database_or_schema_unavailable`; inspect the
   controlled traceback for a missing relation (PostgreSQL SQLSTATE `42P01`).
   `alembic_version` may still be `0001_tasks`: a revision string alone is not
   schema validation. Confirm the renamed object rather than guessing.
4. **Remediate the known drill:** restore the exact original object, preserving
   its rows, indexes and permissions:

   ```bash
   demo_psql <<'SQL'
   BEGIN;
   SET LOCAL lock_timeout = '3s';
   SET LOCAL statement_timeout = '10s';
   SET LOCAL idle_in_transaction_session_timeout = '30s';
   ALTER TABLE public.tasks_drill_backup RENAME TO tasks;
   COMMIT;
   SQL
   ```

   For a **real fresh/unmigrated database**, use the approved environment migration
   job and runtime grants before deploying. For unknown schema corruption,
   escalate to the DBA and a reviewed repair/restore plan. Never `create_all`,
   stamp the revision, drop data or run a blind downgrade to make readiness green.
5. **Verify/reset:** require correct readiness, original task read, CRUD, the
   original table name and no `tasks_drill_backup`. No new table/data should have
   been substituted. Confirm expected revision and original runtime privileges.

## S6. Configuration drift blocks readiness

**Scope/approval:** change only the demo slot's nonsecret expected database name.
Do not change credentials, Key Vault secret values, firewall rules or database
connection URLs. This rehearses a failed configuration gate, not a real DB outage.

1. **Prepare:** record the nonsecret current setting and verify it equals
   `DEMO_DATABASE`. Confirm `DATABASE_NAME` is already slot-sticky:

   ```bash
   demo_webapp config appsettings list \
     --query "[?name=='DATABASE_NAME'].{name:name,value:value,slotSetting:slotSetting}" \
     --output json
   ```

   Never dump the full appsettings response: it can contain secrets.
2. **Inject after approval:**

   ```bash
   demo_webapp config appsettings set \
     --slot-settings DATABASE_NAME=drill_wrong_expected_database --output none
   ```

   App-setting updates can restart the slot. After restart, `/live` should be
   available, `/ready` should return 503 with `database_identity_mismatch`, and
   the reported actual database remains the approved demo DB.
3. **Diagnose:** compare the actual DB identity with the expected nonsecret setting
   and the approved IaC output; inspect the configuration-change Activity Log.
   Normal task reads can still be 200 because the underlying URL has not changed.
   A task 200 alone must not override failed readiness or deployment gates.
4. **Remediate:** restore the recorded approved value, keeping it sticky:

   ```bash
   demo_webapp config appsettings set \
     --slot-settings "DATABASE_NAME=$DEMO_DATABASE" --output none
   ```

   For a real secret/reference problem, separately inspect Key Vault reference
   resolution, secret version/expiry, the slot's identity and approved access.
   Do not print secret values, hard-code a password, or broaden permissions.
5. **Verify/reset:** wait for the same expected artifact to become ready, verify
   the actual/expected DB match, run normal CRUD and confirm the sticky setting
   is restored. A successful configuration API response is not recovery evidence.

## 3. Evidence, ownership and completion

For S3 use the strict [incident/evidence tools](../docs/incident-response.md).
For all other exercises use a **normal manual issue or access-controlled incident
record** with the fields below. The existing automated schema/viewer is intentionally
endpoint-specific; do not invent an S3 fingerprint or fake a Copilot assignment to
fit an infrastructure incident into it.

| Record | Required facts |
|---|---|
| Authorization | Scenario ID; approver; operator; recovery owner; change/window reference |
| Identity | Exact demo resource/DB; exercise and app run IDs; baseline/deployed SHA |
| Impact | Observed failed operation/status; affected environment; unaffected environments checked |
| Detection | UTC injection, first symptom and actual alert times; probe/trace/correlation IDs |
| Diagnosis | Verified cause, relevant resource/activity/telemetry links, alternatives ruled out |
| Remediation | Approved action and actor; command outcome; actual code PR/workflow where applicable |
| Recovery | UTC recovery; original operation verified; readiness/CRUD; fresh telemetry; actual alert state |
| Reset | Settings/grants/table names restored; owned smoke records deleted; no open drill transaction |
| Follow-up | Missing monitor/contract defects; owner and due date; prevention PR/runbook change |

Calculate observed detection and recovery durations from recorded UTC timestamps,
not estimates. If no alert fired, record **not observed**; if no Copilot task ran,
record **not applicable/manual**; if recovery failed, leave the incident open.
Never store raw credentials, tokens, connection strings or task contents as evidence.

The recovery owner signs off only when the original failed operation and common
recovery checks pass, no unintended environment was affected, and every injection
change is reversed. For infrastructure drills, restore the previous response-plan/
handoff configuration **only after** telemetry from the drill has left the
five-minute alert window and any resulting alert/investigation is reconciled.
Preserve earlier records; use a new exercise directory for every attempt.

Stopping a slot does not stop App Service plan charges. Stopped PostgreSQL servers
still incur storage/backup costs; consult
[the cost and teardown guide](../docs/azure-setup.md#10-costs-bounded-usage-and-explicit-teardown).
SRE Agent has always-on plus active-flow costs. More telemetry, availability tests,
Actions and Copilot work can add cost. Do not provision extra capacity or extend
a stalled exercise without the agreed budget/approval.

## References

- [App Service CLI: stop/start/show and slot targeting](https://learn.microsoft.com/en-us/cli/azure/webapp?view=azure-cli-latest)
- [App Service application settings](https://learn.microsoft.com/en-us/cli/azure/webapp/config/appsettings?view=azure-cli-latest)
- [Flexible Server stop/start scope](https://learn.microsoft.com/en-us/azure/postgresql/flexible-server/how-to-stop-start-server-cli)
- [PostgreSQL 16 privilege revocation](https://www.postgresql.org/docs/16/sql-revoke.html)
- [PostgreSQL 16 session signalling](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADMIN-SIGNAL)
- [PostgreSQL 16 locks and transaction lifetime](https://www.postgresql.org/docs/16/explicit-locking.html)
- [Azure Resource Health](https://learn.microsoft.com/en-us/azure/service-health/resource-health-overview)
- [Application Insights availability tests](https://learn.microsoft.com/en-us/azure/azure-monitor/app/availability)
