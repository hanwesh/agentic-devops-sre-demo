# Azure SRE Agent Setup Guide

## Overview
Azure SRE Agent monitors your application through Application Insights and automatically creates GitHub Issues when it detects incidents. This guide covers configuring the agent for this demo.

## Prerequisites
- Azure App Service deployed with Application Insights enabled
- GitHub repository connected to Azure
- SRE Agent access enabled on your Azure subscription

## Step 1: Enable SRE Agent

1. Navigate to the **Azure Portal**
2. Go to your **App Service** → `agentic-devops-demo`
3. In the left menu, find **SRE Agent** (under Monitoring or Diagnose and solve problems)
4. Click **Enable**

## Step 2: Connect to GitHub

1. In the SRE Agent configuration, click **Connect to GitHub**
2. Authorize Azure to access your GitHub account
3. Select the repository: `hanwesh/agentic-devops-sre-demo`
4. Map the App Service resource to the repository

## Step 3: Configure Incident Detection

### Alert Triggers
Configure the following triggers in the SRE Agent:

| Trigger | Condition | Action |
|---------|-----------|--------|
| HTTP 500 Spike | 5xx error rate > 5% for 5 minutes | Create GitHub Issue |
| Slow Response | P95 response time > 3s for 5 minutes | Create GitHub Issue |
| Application Restart | Unexpected restart detected | Create GitHub Issue |
| DB Connection Failure | PostgreSQL connection errors | Create GitHub Issue |

### Issue Configuration
Configure the SRE Agent to include the following in auto-created issues:

1. **Labels**: `sre-incident`, `bug`, `production`
2. **Issue template**: Use the `sre-incident` template (`.github/ISSUE_TEMPLATE/sre-incident.yml`)
3. **Content**: Enable all available sections:
   - Error summary (natural language)
   - Stack trace from Application Insights
   - Performance metrics snapshot
   - Recent deployment information
   - AI-generated root cause analysis
   - Suggested code fix

## Step 4: Configure Application Insights Integration

Ensure Application Insights is properly connected:

```bash
# Verify the connection string is set
az webapp config appsettings list \
  --name agentic-devops-demo \
  --resource-group my-rg \
  --query "[?name=='APPLICATIONINSIGHTS_CONNECTION_STRING'].value" \
  --output tsv
```

The application automatically sends telemetry via `azure-monitor-opentelemetry`:
- HTTP request traces
- Exception details with stack traces
- Custom metrics (response times, DB query times)
- Dependency tracking (PostgreSQL queries)

## Step 5: Test the Integration

1. **Trigger a test incident**:
   ```bash
   ./demo/generate_traffic.sh https://agentic-devops-demo.azurewebsites.net
   ```

2. **Watch Application Insights**:
   - Navigate to Application Insights → Failures
   - You should see 500 errors appearing within 2-3 minutes

3. **Wait for SRE Agent**:
   - The agent typically detects and creates an issue within 5-10 minutes
   - Check GitHub Issues for a new issue with the `sre-incident` label

4. **Verify the full loop**:
   - The SRE Issue Triage workflow should run automatically
   - Labels `priority:high` and `copilot-assigned` should be added
   - Copilot Coding Agent should be assigned

## Troubleshooting

### SRE Agent Not Creating Issues
- Verify the GitHub connection is active in the Azure portal
- Check that Application Insights is receiving telemetry
- Ensure metric alerts are firing (check Alert rules in Azure Monitor)
- Verify the error rate exceeds the configured threshold

### Issues Not Triggering Triage Workflow
- Check that the issue has the `sre-incident` label
- Verify the workflow file `.github/workflows/sre-issue-triage.yml` exists
- Check Actions permissions: Settings → Actions → General → Allow all actions

### Copilot Not Picking Up Issues
- Verify `copilot-assigned` label is added
- Check that Copilot Coding Agent is enabled for the repository
- Review `.github/copilot-instructions.md` for proper configuration
