# Repeatable reliability demo

This is an **opt-in, disposable-branch exercise**, not a permanently broken
application or a production chaos test. Main, staging, and production stay healthy.
Local tests validate the mechanics; they **do not prove the Azure alert → SRE
Agent → issue → Copilot → reviewed fix → recovery loop**.

## What actually breaks

The legacy URL remains `GET /api/tasks?filter=broken`, but:

| Deployment | Expected result |
| --- | --- |
| Scenario disabled, including staging/production | Clear `403`, not an exception |
| Enabled demo, healthy code | `200` task-list JSON; missing status defaults to `pending` |
| Enabled demo, disposable regression commit | Genuine `KeyError` and structured `500` |
| Enabled demo, reviewed restoration | The **same URL and run ID** return `200` |

`src/demo_scenario.py` normally handles an optional status:

```python
return filters.get("status", "pending")
```

The introduction tool changes **only** this known fallback to
`return filters["status"]`. The route calls it with an empty mapping when status is
omitted. Explicit status and ordinary CRUD continue working. This is a real,
type-correct missing-optional-value regression, not an unconditional `raise`, a
type-check suppression, or a test changed to expect failure.

## Prerequisites and approval boundaries

1. Review and merge the reliability baseline, or approve its exact immutable
   commit. Install the repository's development requirements in a Python 3.12+
   virtual environment. Run normal lint, type checks, and the **entire** test
   suite on that healthy baseline.
2. Follow [Azure setup, permissions, isolation, and costs](../docs/azure-setup.md).
   Explicitly opt into the dedicated `demo` slot, its separate database, and its
   separate telemetry. Staging/production must never enable the scenario.
   The slot still shares an App Service plan and PostgreSQL server: use a
   non-production demo deployment, not a plan/server hosting real production work.
   Resource creation, migrations, deployment, and cleanup need an authorized
   operator; none of the branch/traffic tools perform them.
3. Configure the GitHub `demo` environment approval and Azure deployment identity.
   Confirm the exact demo hostname and its deployment metadata before approving
   any traffic. No credentials or connection strings belong in run evidence.
4. Complete [Azure SRE Agent and trusted handoff setup](../docs/sre-agent-setup.md):
   the actual Azure Monitor alert/action group, the SRE Agent's supported alert
   and GitHub connectors, repository access, and Copilot availability/permissions.
   A Bicep deployment or a workflow file alone does **not** connect these services.
   Record the actual alert rule, action group, connector, and repository identities.
5. Agree on the incident fingerprint for the missing-status exception and dedupe
   on **`(demo_run_id, fingerprint)`**. One incident must reconcile to one issue
   and one Copilot task per run, including repeated alert deliveries. A new run ID
   is a new exercise, not permission to duplicate the previous incident.

Do not improvise credentials, bypass environment approvals, lower the alert
threshold, disable telemetry, change the positive tests, or turn off the scenario
to make recovery appear green. If a connector or Copilot assignment is unavailable,
describe the manual handoff honestly and record that the full loop is unverified.

## 1. Show the healthy baseline

Use the normal staging smoke workflow for liveness, schema-aware readiness, and
CRUD. It is not a fault-injection tool. The demo endpoint is deliberately forbidden
on staging.

Show `resolve_demo_status` and the positive contracts:

```bash
.venv/bin/pytest -q tests/test_demo_scenario.py
.venv/bin/pytest -q -m demo_regression
```

There are two `demo_regression` cases:

- `test_demo_status_defaults_when_status_is_missing`
- `test_demo_filter_returns_task_list`

Both pass on healthy code. The endpoint test creates pending/completed records in
the test database and verifies that the optional filter returns the pending task,
not merely an arbitrary HTTP `200`.

## 2. Introduce one regression on a new local branch

Use a clean checkout of the approved baseline. Commit or otherwise preserve your
own changes first; the tool will not discard them. Choose a fresh explicit run ID:
1–48 lowercase letters/digits/hyphens, starting and ending with a letter or digit.
The approved base must be a full 40-character commit SHA, not a moving branch name.

```bash
RUN_ID="presenter-20260920-01"  # Choose a different ID for every exercise.
BASE_SHA="<approved healthy full commit SHA>"
.venv/bin/python -m scripts.demo_branch introduce \
  --run-id "$RUN_ID" --base "$BASE_SHA" \
  --environment demo --acknowledge-disposable-demo
```

This creates **and switches to `demo/<run-id>` and creates a local introduction
commit**. Its JSON output includes the commit SHA. It does not push or deploy.
It refuses dirty/untracked work, ongoing Git operations, an existing local or
fetched remote-tracking run branch, an unknown helper implementation, or a
missing/ambiguous base commit. Human approval of that SHA remains the operator's
responsibility. Fetch approved refs before the exercise; the
tool itself performs no network operations. A failed Git hook/signing/commit
operation leaves work for inspection, never a destructive automatic rollback.

Inspect the diff: only the optional-filter fallback may change. Tests,
configuration, feature flags, alert thresholds, and workflows must not change.

Normal CI must now fail the unchanged positive tests. The controlled demo
deployment is the sole exception, and must:

- keep lint, type checking, and `pytest -m "not demo_regression"` as hard gates;
- separately run the two exact positive cases, check their collected test IDs,
  and verify **only those expected regression failures**;
- fail on zero collected cases, unrelated failures, collection errors, or an
  unexpectedly healthy regression branch;
- accept only the matching disposable `demo/<run-id>` branch and pin the resolved
  source/artifact SHA.

A blanket `continue-on-error`, an unchecked failing command, or skipping all
tests is not an acceptable demonstration. The isolated Git test in
`tests/test_demo_branch.py` also proves the unchanged positive helper test passes,
fails after introduction, then passes after forward restoration.

## 3. Approve the dedicated demo deployment

Only after inspecting the exact diff, an authorized operator can publish the
disposable branch (a normal push, **never force push**) and dispatch
`.github/workflows/demo.yml` **from the current default branch (`main`)**.
The `demo` GitHub environment permits only that workflow ref and requires a
reviewer with self-review prevented; it must not allow arbitrary `demo/*` workflow
revisions to acquire deployment credentials.

Supply `branch=demo/<run-id>`, `run_id=<run-id>`, `scenario_state=regression`,
and `issue_number=0`. For recovery, use `scenario_state=healthy` and
`issue_number=<original-incident-number>`. The workflow resolves the candidate
to an exact SHA separately from the trusted workflow ref and must use the dedicated demo
database/telemetry and bind evidence to the deployed commit.

Before any fault request, both `/live` and `/ready` must report:

- `environment: "demo"`;
- `demo_scenario_enabled: true`;
- the expected `demo_run_id` and full `commit_sha`;
- correct liveness/readiness state, a healthy database, and the expected schema.

Keep `DEMO_SCENARIO_ENABLED=true` during **both** regression and healthy recovery.
The code restoration is the remediation.

## 4. Send a bounded run and retain its evidence

The default is 30 scenario requests plus six liveness/readiness/control requests,
with a 120-second total deadline and five-second per-request timeout. The driver
allows 21–100 scenario requests, a maximum total deadline of 240 seconds, and a
maximum request timeout of ten seconds. There are no automatic retries.

```bash
mkdir -p demo/evidence
.venv/bin/python -m demo.traffic \
  --base-url "https://<app>-demo.azurewebsites.net" \
  --run-id "$RUN_ID" --expected-commit "<deployed regression full SHA>" \
  --mode fault --allow-faults \
  --requests 30 --max-duration 120 --timeout 5 \
  --evidence "demo/evidence/$RUN_ID-fault.json"
```

`demo/generate_traffic.sh` is a thin wrapper for the same CLI; set
`PYTHON=.venv/bin/python` when using it. There is no unsafe positional-URL mode.

Safety properties:

- Only an HTTPS `*-demo.azurewebsites.net` hostname or explicit loopback is allowed.
  Production/staging hostnames, URL credentials, paths, query strings, ambiguous
  URLs, redirects, and environment-proxy routing are not allowed.
- Both identity endpoints are checked **before** the scenario, and again after it.
  Every response must carry the expected deployment SHA and echoed correlation
  ID. Azure targets also require a matching trace ID; missing instrumentation is
  a failure, not evidence of an operational monitoring loop.
- Normal task-list controls must stay `200`. Fault requests must be actual `500`
  responses with the expected `KeyError` JSON envelope. Healthy-mode requests must
  be `200` with the real paginated task-list shape and default pending status.
- The process exits nonzero immediately on an unexpected result or expired
  deadline. A refused or failed run must never be presented as successful.
- Traffic is **GET-only**: no seed records, no mutations, no bulk cleanup, and no
  lingering tasks. Evidence contains metadata/status/correlation/trace IDs, not
  task contents, response bodies, cookies, tokens, or connection strings.
- Evidence files must be new relative files inside the checkout and are never
  overwritten. Use a new attempt filename for an authorized retry of the same run.
  `demo/evidence/` is ignored by Git; retain required artifacts in the workflow.

The driver's `eligible_request_count` and `error_ratio` include only task controls
and scenario requests, excluding all liveness/readiness probes, matching the
alert's denominator. Thirty fault responses in at most four minutes provide more
than twenty samples and an observed ratio above 5%. This is **not a guarantee that Azure
has fired an alert**: ingestion/sampling, other traffic in the denominator,
evaluation windows, and action-group delivery still require verification. Do not
run an unbounded loop while waiting or alter the alert threshold to manufacture
success.

## 5. Observe the real handoff

Use the recorded UTC interval, deployment SHA, run ID, correlation IDs, and trace
IDs to find the actual request and exception rows in the demo telemetry resource.
Verify the missing `status` stack frame in `resolve_demo_status`.

Then capture, rather than assume:

1. The firing Azure Monitor alert and its measured sample count/error ratio.
2. Action-group delivery and the SRE Agent incident/task identity.
3. The trusted, structured GitHub issue for this fingerprint and run, including
   source SHA, affected endpoint, telemetry evidence, and reproduction.
4. Successful triage and the actual Copilot task/assignment, or an explicitly
   documented manual boundary if that integration is unavailable.

Repeated notifications should reconcile with the existing issue/task. Replaying
untrusted issue prose must not grant an arbitrary branch, repository, URL, or
deployment authority.

## 6. Review the fix and verify recovery on the same run

The fix PR must target **`demo/<run-id>`, not main**. Restore correct handling of
the missing optional status without disabling the flag or changing the contract.
Review the one-line restoration (or an equivalently correct reviewed code fix)
and the unchanged positive tests. Full, unfiltered CI must pass before a human
approves and merges into that disposable branch.

Dispatch the trusted demo workflow with the same branch/run ID, state `healthy`,
and original issue number. Record the new deployed artifact SHA. Its recovery
check must call the formerly failing URL, not just `/health`.

```bash
.venv/bin/python -m demo.traffic \
  --base-url "https://<app>-demo.azurewebsites.net" \
  --run-id "$RUN_ID" --expected-commit "<deployed reviewed fix full SHA>" \
  --mode healthy --allow-faults \
  --evidence "demo/evidence/$RUN_ID-recovery.json"
```

Require successful pre/post readiness and identity, ordinary task controls, and
all scenario requests returning valid `200` task lists while the scenario remains
enabled. Attach this evidence to the existing incident. Then verify fresh Azure
telemetry, the alert's resolved state after its evaluation window, and the SRE
Agent's recovery acknowledgement. Keep the incident unverified/open when that
external evidence is missing. A PR's `Fixes #...` text on a non-default demo branch
does not itself prove recovery or reliably close the issue.

**Completion evidence:** approved baseline → regression commit/deployment →
bounded fault report → telemetry/alert/action delivery → one issue/task →
reviewed fix PR → healthy deployment/report for the same run → external
alert/SRE recovery. The driver always reports `azure_loop_verified: false`;
it cannot independently observe that complete external chain.

## Reset or repeat without erasing history

If the exercise is aborted before a reviewed fix, or the exact known regression
still exists, run this only on its clean disposable branch:

```bash
.venv/bin/python -m scripts.demo_branch reset \
  --run-id "$RUN_ID" --environment demo --acknowledge-disposable-demo
```

`reset` means a **forward restoration commit**, not `git reset --hard`, checkout
of old files, branch deletion, or history rewriting. Only the known helper
fallback is restored. If it is already restored, the command reports
`changed: false`; an unfamiliar helper is refused for human review. Inspect it,
run full CI, and follow the same approved healthy deployment/recovery procedure.
This local operation does not deploy, close issues, or claim incident resolution.

For run two, choose a **new** explicit run ID and use `introduce` from the same
approved healthy base SHA (or a newly reviewed healthy baseline). Do not reuse or
delete the old branch to trick the guard. Keep separate immutable evidence files
and ensure the earlier alert window/incident is resolved before presenting a new
one. No task data needs cleanup because the traffic driver never creates any.
Any optional resource teardown follows the approvals and cost guidance in
[Azure setup](../docs/azure-setup.md), never a blanket deletion of shared resources.
