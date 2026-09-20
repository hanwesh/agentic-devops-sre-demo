# Azure SRE Agent: operator setup

Azure SRE Agent and GitHub Copilot cloud agent are **separate services**. An App
Service deployment does not create an SRE Agent, authorize a GitHub connector,
enable a response plan, or assign Copilot. This repository supplies validation,
handoff, and evidence tooling; an operator must complete and verify the integrations
below. No setup step in this document has been performed by the reliability PR.

The target is the isolated **demo slot and demo database**, never production.
Keep `SRE_HANDOFF_ENABLED` unset until every checkpoint passes. Missing access,
consent, telemetry, or agent eligibility means **blocked/manual fallback**, not a
successful automated loop.

The [failure-scenario playbook](../demo/failure-scenarios.md) also covers app/database
outages, locks, missing schema and configuration drift. Pause the code-regression
publisher/handoff for those operational drills; keep investigation read-only and
use their separately approved recovery steps. The existing request alerts cannot
detect a stopped app that emits no telemetry.

## 1. Approve scope, access, and costs

Before provisioning, record the demo App Service slot, demo database, demo
Application Insights resource, Log Analytics workspace, resource group, repository,
and accountable operator. Use the resource outputs from [Azure setup](azure-setup.md);
do not substitute a production resource when an output is missing.

For the SRE Agent service itself, choose **one** provisioning path:

- Opt into the published `Microsoft.App/agents@2026-01-01` Bicep module described
  in [Azure setup](azure-setup.md#7-optional-sre-agent-and-operator-boundaries).
  Supply the approved region/model/provider/consent reference. Reader assignments
  are a separate opt-in; connectors and response plans remain manual.
- Or use the portal procedure below for an operator-managed agent. Do not create
  a second billable agent if IaC has already provisioned the intended resource.

1. Open [sre.azure.com](https://sre.azure.com), sign in, and select **Create agent**.
   This creates a standalone Azure SRE Agent resource; it is not an App Service
   "Enable SRE Agent" switch.
2. Follow **Basics → Review → Deploy**. Choose a supported subscription, resource
   group, region, and model provider. Providers depend on subscription and region.
   Check live availability; the FAQ currently lists Sweden Central, East US 2,
   and Australia East, and permits investigation of resources in other regions.
3. The creation guide requires Owner, or Contributor plus User Access
   Administrator, on the creation scope. Activate required PIM roles before
   granting access. Creating an agent is an approved infrastructure write.
4. Review the deployment and managed identities. Current setup creates system-
   and user-assigned identities; use the **UAMI** for connector authentication and
   resource RBAC as described by the service.
5. Set an approved active-flow consumption limit. Pricing includes an always-on
   charge (currently 4 AAUs per agent-hour) and model/token-dependent active flow.
   Azure Monitor ingestion, database/App Service hosting, GitHub Actions, and
   Copilot AI-credit usage can add costs. A stopped agent still incurs always-on
   charges; deletion stops those charges. Consult current pricing before consent.

**Checkpoint:** a real agent resource exists, costs are accepted, and its scope is
recorded. Opt-in IaC and a guarded operator teardown are supplied, but neither has
been executed for this PR.

## 2. Connect telemetry and code explicitly

In **Set up your agent**, use the Code and Logs cards; later manage connections
under **Builder → Connectors**.

### Read-only investigation

- Add the **demo** Application Insights and/or Log Analytics source. Validate a
  narrow time-bounded query before continuing. The portal can assign Log Analytics
  Reader and Monitoring Reader when saving these connectors.
- If Azure resource inspection is needed, choose the demo resource group and
  **Reader** access, not Privileged. Review *all* proposed role assignments: the
  service may add resource-specific operator roles beyond base Reader roles.
  Do not approve production or subscription-wide write access merely to complete
  this demo.
- Built-in Azure queries use managed identity/RBAC. A Logs connector adds persistent
  context; it does not replace permission checks.
- The agent needs private-network access if telemetry endpoints are private.
  Configure supported network connectivity through the operator process. Never
  make a database public as an automated workaround.

### GitHub connector and consent

1. On the Code card, select **Connect repositories**, choose GitHub, and use an
   authentication option actually offered by your tenant: account/OAuth, PAT, or
   a bring-your-own GitHub App. Select only the intended repository.
2. An SRE Agent Administrator configures connectors. The external GitHub account
   must separately consent and have appropriate repository/organization access.
3. Verify connector health and a read-only repository/issue lookup. The exact
   connector name, available tools, and identity are tenant-dependent; there is
   no repository YAML here that silently configures or authorizes them.
4. Keep investigation tools read-only. Issue creation/update is a **separate
   approved write** by a narrowly scoped publisher. Record its exact GitHub login
   in `SRE_ALLOWED_ISSUE_AUTHOR`. Repository membership, `author_association`, a
   convincing display name, or an `sre-incident` label is not an allowlist.
5. Give the publisher only repository metadata read and issue write access unless
   another documented operation requires more. Do not give it the Copilot token.
   Do not automatically expose all current and future connector tools via a
   wildcard.

**Checkpoint:** telemetry reads and repository reads actually work. If issue-write
tooling cannot run the strict, serialized publishing procedure in the
[response runbook](incident-response.md), require an operator to publish instead.
Do not claim that merely installing a connector creates incident issues.

## 3. Connect Azure Monitor as an incident platform

Connectors provide tools/data; an **incident platform** supplies incoming alerts.
In **Builder → Incident platform**, choose Azure Monitor and save. Only one
incident platform can be active at a time.

The documented Azure Monitor integration acknowledges alerts and merges repeated
firings of the same alert rule into an investigation thread. These are writes,
separate from read-only log queries, and require explicit operator consent.

**Permission caveat:** the Azure Monitor integration's troubleshooting guide
currently lists Monitoring Contributor on the subscription for scanner access.
This is broader than Reader on a demo resource group. If your tenant cannot
receive the narrowly scoped alerts without that grant, stop and obtain a
separately reviewed scope/role decision (prefer an isolated demo subscription),
or use manual incident intake. Do not silently escalate permissions or claim
automatic detection works. No role/policy changes are made by this PR.

After connecting, inspect **Builder → Incident response plans → Table view**.
The service may create a `quickstart_handler` plan in **Autonomous** mode covering
many services. Have an operator disable/remove that overlapping plan before
enabling the demo plan, otherwise incidents may be processed twice or outside
the intended scope.

Configure the operator-owned plan from [incident-response.md](incident-response.md):

- Exact demo service/resource and demo alert-rule title filters.
- Azure severity values appropriate for the configured alerts.
- A dedicated investigator with only the needed telemetry/code-read tools.
- **Review** autonomy, not the wizard's Autonomous default.
- Approved issue publishing separated from investigation and all Azure
  remediation/deployment writes.

The infrastructure alert contract is at least **20 observed nonprobe records in a five-minute
window**, with a **weighted 5xx ratio above 5% or p95 duration above 3000 ms**. Low volume
or missing telemetry is insufficient evidence, never proof of recovery. Confirm
the deployed log-query alert rules and dimensions rather than relying on the
response-plan filters to calculate these thresholds.

**Checkpoint:** an actual demo alert is visible in the SRE Agent incident view,
routes to only the intended Review-mode plan, and can be investigated. This
verification needs a separately authorized demo exercise; mocked tests do not
prove that a live Azure integration is configured.

## 4. Configure the supported Copilot handoff

### Authentication and policy gates

GitHub's documented tasks and issue-assignment integrations are in **public
preview**. The tasks API accepts user-to-server PAT/OAuth/GitHub App user tokens,
**not installation access tokens**. Issue assignment likewise requires a user
token. An Actions `GITHUB_TOKEN` or App installation token is not a replacement.

This implementation uses **issue assignment**, not the tasks API, because live
issue assignees provide a documented reconciliation read. Do not fall back from
an ambiguous issue assignment to a task creation: that could launch duplicate,
billable work.

An authorized operator must first confirm Copilot entitlement, repository access,
organization policy, and cost consent. The workflow additionally queries the
repository's `suggestedActors` using the user token and requires
`copilot-swe-agent`. This check does not modify policy.

Configure these values only after approval:

| Setting | Meaning |
| --- | --- |
| Repository variable `SRE_HANDOFF_ENABLED` | Exact value `true`; otherwise fail closed |
| Repository variable `COPILOT_POLICY_APPROVED` | Exact value `true`, attesting the operator's policy/cost review |
| Repository variable `SRE_ALLOWED_ISSUE_AUTHOR` | Exact verified issue-publisher login, including `[bot]` if applicable |
| Repository variable `SRE_DEMO_APP_INSIGHTS_RESOURCE_ID` | Exact demo component ARM resource ID, not a connection string |
| Actions secret `COPILOT_USER_TOKEN` | Approved, expiring/rotated user-to-server token, separate from `GITHUB_TOKEN` |

The official issue-assignment guide specifies metadata read plus **Actions,
Contents, Issues, and Pull requests read/write** for a fine-grained PAT, or `repo`
for a classic PAT. Limit the token to this repository, meet organization approval/
SSO requirements, and rotate/revoke through the normal operator process. A GitHub
App **user** token is distinct from the App's installation token.

Ordinary workflow metadata writes use the short-lived `GITHUB_TOKEN` with
`issues: write` and `contents: write`. Contents write is needed **only** for an
atomic launch-reservation tag. The workflow checks out trusted default-branch
code with persisted checkout credentials disabled; no issue prose becomes a shell
script, executable expression, or checkout ref.

### Exact API contract

The implementation uses the official documented request:

```text
POST /repos/{owner}/{repo}/issues/{issue_number}/assignees
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Authorization: Bearer <COPILOT_USER_TOKEN>
```

Its JSON contains `assignees: ["copilot-swe-agent[bot]"]` and `agent_assignment`
with `target_repo`, `base_branch: "demo/<scenario_run_id>"`, a **fixed** trusted
instruction template, and empty `custom_agent`/`model` defaults. It never copies
arbitrary issue text into `custom_instructions`.

`201 Created` is not sufficient: the general assignees API can silently ignore an
assignee. The workflow reads the issue again, bounded to three reads, and requires
the actual Copilot assignee. Only then does it post accepted evidence and add
`copilot-assigned`/`priority:*`. The linked evidence is the **real issue URL**;
the timeline reports a PR as pending until a real same-repository demo-branch PR
is linked.

### Durable, fail-closed idempotency

- Actions serializes per issue, with `cancel-in-progress: false`.
- Before assignment, the client atomically creates an annotated
  `refs/tags/sre-handoff/<full-fingerprint>` reference. The annotation binds the
  fingerprint, issue number, incident digest, commit, and real creation time.
  It must also claim `refs/tags/sre-handoff-issues/<issue-number>` pointing to the
  same annotation, so editing an issue to a new fingerprint cannot bypass an
  earlier unresolved launch.
  GitHub's create-reference uniqueness also guards concurrent clients and duplicate
  issues sharing a fingerprint.
- The reservation is **never automatically deleted, expired, overwritten, or
  retried as a new launch**. A crash before the assignment may sacrifice automatic
  progress rather than risk duplicate paid work.
- If assignment succeeded but evidence/comment publication failed, rerunning
  reconciles the live assignee and writes evidence without another assignment.
- If the outcome remains unknown, authentication fails, eligibility is missing,
  or an API fails, the command exits nonzero. Follow the manual runbook; no success
  label is added.

Protect both reservation namespaces operationally; do not routinely delete these
tags. A repository administrator can defeat any repository-local dedupe by
deleting records, so cleanup requires a human audit. Ordinary evidence recording
does **not** need contents write.

## 5. Human review, deployment, and recovery

Copilot acceptance is not a fix, test pass, merge, deployment, or recovery.

By default, workflows triggered by a Copilot PR need a maintainer to inspect its
changes and select **Approve and run workflows**. Preserve that safeguard.
Repository rulesets may also prevent Copilot work; report the incompatibility
rather than granting an automatic bypass. This PR does not change repository
policies, approval requirements, or environment protections.

The sole `copilot-setup-steps` job installs the constrained Python environment
and exercises migrations/CRUD against a disposable PostgreSQL service. It takes
effect for cloud-agent work after this file is on the default branch; a failed
setup step does not guarantee the agent stops. Configure only test-safe variables
in the `copilot` environment if needed for subsequent commands:
`TEST_POSTGRES_URL` pointing to that ephemeral localhost service and
`ALLOW_EPHEMERAL_POSTGRES=true`. Never add Azure, production DB, or Copilot launch
credentials there. An absent test URL means an explicit skip, not an integration pass.

Review the fix and regression test, confirm the PR targets `demo/<run-id>`, and
use the protected demo deployment process. Then verify the **original**
`GET /api/tasks?filter=broken` endpoint and deployment identity, not just `/health`
or `/ready`.
Use the [evidence commands](incident-response.md#durable-deployment-and-recovery-evidence)
to preserve deployment/recovery artifacts and issue comments. The viewer has no
application write endpoint, frontend service, or process-local source of truth.

## Sources and verification

Official sources reviewed on **2026-09-20**; preview behavior and billing can change:

- [Create and set up Azure SRE Agent](https://learn.microsoft.com/en-us/azure/sre-agent/create-and-set-up)
- [Connectors and consent](https://learn.microsoft.com/en-us/azure/sre-agent/connectors)
- [General FAQ and availability](https://learn.microsoft.com/en-us/azure/sre-agent/faq)
- [Pricing and billing](https://learn.microsoft.com/en-us/azure/sre-agent/pricing-billing)
- [Incident platforms](https://learn.microsoft.com/en-us/azure/sre-agent/incident-platforms)
- [Azure Monitor alerts and scanner prerequisites](https://learn.microsoft.com/en-us/azure/sre-agent/azure-monitor-alerts)
- [Incident response plans and quickstart overlap](https://learn.microsoft.com/en-us/azure/sre-agent/incident-response-plans)
- [Cloud-agent API authentication and exact requests](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api)
- [Supported session entry points](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/start-copilot-sessions)
- [Issue-assignee response semantics](https://docs.github.com/en/rest/issues/assignees#add-assignees-to-an-issue)
- [Atomic reference creation](https://docs.github.com/en/rest/git/refs#create-a-reference)
- [Annotated tag objects](https://docs.github.com/en/rest/git/tags#create-a-tag-object)
- [Copilot workflow approval](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/configuring-agent-settings)

All repository validation for this integration uses mocked GitHub transports.
No test assigns Copilot, provisions Azure, sends live fault traffic, or proves
live recovery.
