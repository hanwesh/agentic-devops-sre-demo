# Demo Presenter's Guide

## Overview
This guide walks you through the **Agentic DevOps & SRE Demo**, showing how Azure SRE Agent, GitHub Actions, and Copilot Coding Agent work together in a self-healing development loop.

## Pre-Demo Setup

### 1. Deploy Azure Infrastructure
```bash
cd infrastructure
./deploy.sh <resource-group> <location>
```

### 2. Configure GitHub Secrets
In your repository settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `AZURE_CLIENT_ID` | From Azure AD app registration |
| `AZURE_TENANT_ID` | Your Azure AD tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Your Azure subscription ID |

| Variable | Value |
|----------|-------|
| `AZURE_WEBAPP_NAME` | `agentic-devops-demo` |
| `AZURE_RESOURCE_GROUP` | Your resource group name |

### 3. Configure Azure SRE Agent
1. In the Azure portal, navigate to your App Service
2. Enable Azure SRE Agent integration
3. Link to this GitHub repository
4. Configure incident triggers (HTTP 500 rate > 5%)

### 4. Verify the App is Running
```bash
curl https://agentic-devops-demo.azurewebsites.net/health
# Should return: {"status": "healthy", ...}
```

---

## Demo Script (10-15 minutes)

### Act 1: The Healthy Application (2 min)
1. Open the Swagger UI: `https://<app>.azurewebsites.net/docs`
2. Show the Task API — create, list, update, delete tasks
3. Show the health endpoint returning "healthy"
4. Show Application Insights dashboard — all green

### Act 2: The Bug is Deployed (2 min)
1. Explain: "A developer merged a PR with a bug. The `/api/tasks?filter=broken` endpoint now crashes."
2. Show the buggy code in `src/routes/tasks.py` — the `filter=broken` path
3. Show that CI/CD deployed it automatically

### Act 3: Traffic Triggers the Incident (2 min)
1. Run the traffic generator:
   ```bash
   ./demo/generate_traffic.sh https://<app>.azurewebsites.net
   ```
2. Watch Application Insights — error rate climbing
3. Wait for the 5xx alert to fire

### Act 4: SRE Agent Creates an Issue (3 min)
1. Switch to GitHub → Issues tab
2. Show the auto-created issue from Azure SRE Agent
3. Walk through the rich content:
   - Error summary
   - Stack trace
   - Performance metrics
   - Root cause analysis
   - Suggested fix

### Act 5: Triage & Assignment (1 min)
1. Show GitHub Actions → SRE Issue Triage workflow running
2. Show the labels added: `sre-incident`, `priority:high`, `copilot-assigned`
3. Show the triage comment posted

### Act 6: Copilot Creates the Fix (3 min)
1. Show the Copilot Coding Agent picking up the issue
2. Show the PR created by Copilot:
   - Proper null-handling fix in `src/routes/tasks.py`
   - New test covering the edge case
   - PR references `Fixes #<issue-number>`
3. Show CI passing on the PR

### Act 7: Human Review & Resolution (2 min)
1. Review the PR — approve and merge
2. Show CD deploying the fix
3. Hit the previously broken endpoint — now works
4. Show Application Insights — error rate dropping
5. Show Azure SRE Agent confirming resolution

---

## Key Talking Points
- **Zero human intervention** from incident detection to fix PR
- **AI-powered RCA** — SRE Agent identifies the root cause, not just symptoms
- **Quality gates preserved** — CI runs on the fix PR, human reviews before merge
- **Full observability loop** — the same monitoring that detected the bug confirms the fix
- **Standard tooling** — GitHub Actions, Azure App Service, Application Insights (no exotic tools)
