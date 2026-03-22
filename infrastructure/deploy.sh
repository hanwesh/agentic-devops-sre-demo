#!/usr/bin/env bash
# Deploy Azure infrastructure using Bicep
# Usage: ./deploy.sh <resource-group> <location> [postgres-password]

set -euo pipefail

RESOURCE_GROUP="${1:?Usage: ./deploy.sh <resource-group> <location> [postgres-password]}"
LOCATION="${2:?Usage: ./deploy.sh <resource-group> <location> [postgres-password]}"
POSTGRES_PASSWORD="${3:-}"

if [ -z "$POSTGRES_PASSWORD" ]; then
  echo "Enter PostgreSQL admin password:"
  read -rs POSTGRES_PASSWORD
fi

echo "🏗️  Creating resource group: $RESOURCE_GROUP in $LOCATION..."
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output none

echo "🚀 Deploying Bicep template..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infrastructure/main.bicep \
  --parameters \
    baseName=agentic-devops-demo \
    postgresAdminPassword="$POSTGRES_PASSWORD" \
  --output json

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Next steps:"
echo "  1. Configure GitHub repository secrets (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID)"
echo "  2. Configure GitHub repository variables (AZURE_WEBAPP_NAME, AZURE_RESOURCE_GROUP)"
echo "  3. Set up OIDC federated credential in Azure AD"
echo "  4. Configure Azure SRE Agent to monitor the App Service"
echo "  5. Push code to main branch to trigger CI/CD"
