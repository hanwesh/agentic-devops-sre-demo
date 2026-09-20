// Published resource schema: Microsoft.App/agents@2026-01-01, reviewed 2026-09-20.
// OAuth consent, repository access, user roles, and response plans are NOT configured here.
param name string

@allowed(['australiaeast', 'eastus2', 'swedencentral'])
param location string

@minLength(1)
param modelName string
@minLength(1)
param modelProvider string

@description('Operator change reference only; does not grant service/provider consent.')
@minLength(1)
param consentReference string

param grantInvestigationRoles bool = false
param appName string
param demoWorkspaceName string
param demoInsightsName string
param tags object

resource demoApp 'Microsoft.Web/sites@2023-12-01' existing = {
  name: appName
}
resource demoSlot 'Microsoft.Web/sites/slots@2023-12-01' existing = {
  parent: demoApp
  name: 'demo'
}
resource demoWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: demoWorkspaceName
}
resource demoInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: demoInsightsName
}

resource investigationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${name}-investigator'
  location: location
  tags: tags
}

resource agentWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${name}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource agentInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${name}-insights'
  location: location
  kind: 'web'
  tags: tags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: agentWorkspace.id
  }
}

resource agent 'Microsoft.App/agents@2026-01-01' = {
  name: name
  location: location
  tags: union(tags, { 'consent-review-reference': consentReference })
  identity: {
    type: 'SystemAssigned,UserAssigned'
    userAssignedIdentities: {
      '${investigationIdentity.id}': {}
    }
  }
  properties: {
    actionConfiguration: {
      mode: 'ReadOnly'
      accessLevel: 'Low'
      identity: investigationIdentity.id
    }
    defaultModel: {
      name: modelName
      provider: modelProvider
    }
    knowledgeGraphConfiguration: {
      identity: investigationIdentity.id
      // The resource picker uses RG scopes; this RG also contains production.
      // Do not auto-connect it or let the portal broaden the demo-only RBAC.
      managedResources: []
    }
    logConfiguration: {
      applicationInsightsConfiguration: {
        appId: agentInsights.properties.AppId
        connectionString: agentInsights.properties.ConnectionString
      }
    }
    upgradeChannel: 'Stable'
  }
}

var readerRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')
var monitoringReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '43d0d8ad-25c7-4714-9337-8ba259a9fe05')
var logReaderRole = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')

resource slotReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantInvestigationRoles) {
  name: guid(demoSlot.id, investigationIdentity.id, readerRole)
  scope: demoSlot
  properties: {
    roleDefinitionId: readerRole
    principalId: investigationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource telemetryReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantInvestigationRoles) {
  name: guid(demoInsights.id, investigationIdentity.id, monitoringReaderRole)
  scope: demoInsights
  properties: {
    roleDefinitionId: monitoringReaderRole
    principalId: investigationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource logsReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (grantInvestigationRoles) {
  name: guid(demoWorkspace.id, investigationIdentity.id, logReaderRole)
  scope: demoWorkspace
  properties: {
    roleDefinitionId: logReaderRole
    principalId: investigationIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output agentResourceId string = agent.id
output investigationPrincipalId string = investigationIdentity.properties.principalId
