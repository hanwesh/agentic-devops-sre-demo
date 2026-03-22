# Azure Setup Guide

## Prerequisites
- Azure CLI installed and logged in (`az login`)
- An Azure subscription with sufficient permissions
- A GitHub repository (this one)

## Step 1: Deploy Infrastructure

```bash
# Clone the repo
git clone https://github.com/hanwesh/agentic-devops-sre-demo.git
cd agentic-devops-sre-demo

# Make the deploy script executable
chmod +x infrastructure/deploy.sh

# Deploy (you'll be prompted for the PostgreSQL password)
./infrastructure/deploy.sh my-rg eastus
```

This deploys:
- Azure App Service (Standard S1, Linux, Python 3.12)
- Azure Database for PostgreSQL Flexible Server (Standard_D2s_v3)
- Application Insights + Log Analytics Workspace
- Metric alerts for 5xx errors and slow response times
- Staging deployment slot

## Step 2: Set Up OIDC Authentication

GitHub Actions uses OpenID Connect (OIDC) to authenticate with Azure — no stored secrets needed.

### 2a. Create an Azure AD App Registration
```bash
az ad app create --display-name "agentic-devops-demo-github"
```
Note the `appId` (this is your `AZURE_CLIENT_ID`).

### 2b. Create a Service Principal
```bash
az ad sp create --id <appId>
```

### 2c. Assign Role
```bash
az role assignment create \
  --assignee <appId> \
  --role Contributor \
  --scope /subscriptions/<subscriptionId>/resourceGroups/my-rg
```

### 2d. Configure Federated Credential
```bash
az ad app federated-credential create \
  --id <appId> \
  --parameters '{
    "name": "github-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:hanwesh/agentic-devops-sre-demo:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Also create credentials for environments:
```bash
# For staging environment
az ad app federated-credential create \
  --id <appId> \
  --parameters '{
    "name": "github-staging",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:hanwesh/agentic-devops-sre-demo:environment:staging",
    "audiences": ["api://AzureADTokenExchange"]
  }'

# For production environment
az ad app federated-credential create \
  --id <appId> \
  --parameters '{
    "name": "github-production",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:hanwesh/agentic-devops-sre-demo:environment:production",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

## Step 3: Configure GitHub Repository

### Secrets (Settings → Secrets → Actions)
| Secret | Value |
|--------|-------|
| `AZURE_CLIENT_ID` | App registration's Application (client) ID |
| `AZURE_TENANT_ID` | Azure AD Tenant ID |
| `AZURE_SUBSCRIPTION_ID` | Azure Subscription ID |

### Variables (Settings → Variables → Actions)
| Variable | Value |
|----------|-------|
| `AZURE_WEBAPP_NAME` | `agentic-devops-demo` |
| `AZURE_RESOURCE_GROUP` | `my-rg` |

### Environments
Create two environments in Settings → Environments:
1. **staging** — no protection rules
2. **production** — require reviewer approval

## Step 4: Run Initial Deployment

Push to `main` to trigger the CD pipeline:
```bash
git push origin main
```

Monitor the workflow at: `https://github.com/hanwesh/agentic-devops-sre-demo/actions`

## Step 5: Verify

```bash
# Check the app is running
curl https://agentic-devops-demo.azurewebsites.net/health

# Expected:
# {"status":"healthy","environment":"production","database":"healthy",...}
```

## Resource Costs (Estimated)
| Resource | Tier | Estimated Monthly Cost |
|----------|------|----------------------|
| App Service Plan | Standard S1 | ~$73/month |
| PostgreSQL Flexible Server | Standard_D2s_v3 | ~$125/month |
| Application Insights | Pay-as-you-go | ~$5-15/month |
| Log Analytics | Pay-as-you-go | ~$2-5/month |
| **Total** | | **~$205-218/month** |

> 💡 **Tip**: For cost savings during non-demo periods, stop the App Service and PostgreSQL server.
