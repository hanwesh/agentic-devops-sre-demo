// Azure Infrastructure for Agentic DevOps SRE Demo
// Deploys: App Service + PostgreSQL + Application Insights + Log Analytics

targetScope = 'resourceGroup'

@description('Base name for all resources')
param baseName string = 'agentic-devops-demo'

@description('Azure region for resources')
param location string = resourceGroup().location

@description('PostgreSQL administrator login')
param postgresAdminLogin string = 'pgadmin'

@secure()
@description('PostgreSQL administrator password')
param postgresAdminPassword string

@description('App Service Plan SKU')
param appServicePlanSku string = 'S1'

@description('PostgreSQL SKU name')
param postgresSku string = 'Standard_D2s_v3'

@description('PostgreSQL storage size in GB')
param postgresStorageGB int = 32

// ── Log Analytics Workspace ───────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${baseName}-logs'
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ── Application Insights ──────────────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${baseName}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── App Service Plan (Linux, Standard S1) ─────────────────────────────
resource appServicePlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${baseName}-plan'
  location: location
  kind: 'linux'
  sku: {
    name: appServicePlanSku
    tier: 'Standard'
  }
  properties: {
    reserved: true // Required for Linux
  }
}

// ── App Service ───────────────────────────────────────────────────────
resource appService 'Microsoft.Web/sites@2023-12-01' = {
  name: baseName
  location: location
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'uvicorn src.main:app --host 0.0.0.0 --port 8000'
      alwaysOn: true
      healthCheckPath: '/health'
      appSettings: [
        {
          name: 'DATABASE_URL'
          value: 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/tasksdb?ssl=require'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'ENVIRONMENT'
          value: 'production'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
      ]
    }
  }
}

// ── Staging Slot ──────────────────────────────────────────────────────
resource stagingSlot 'Microsoft.Web/sites/slots@2023-12-01' = {
  parent: appService
  name: 'staging'
  location: location
  properties: {
    serverFarmId: appServicePlan.id
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'uvicorn src.main:app --host 0.0.0.0 --port 8000'
      alwaysOn: true
      healthCheckPath: '/health'
      appSettings: [
        {
          name: 'DATABASE_URL'
          value: 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/tasksdb?ssl=require'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'ENVIRONMENT'
          value: 'staging'
        }
        {
          name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
          value: 'true'
        }
      ]
    }
  }
}

// ── Azure Database for PostgreSQL Flexible Server ─────────────────────
resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: '${baseName}-pg'
  location: location
  sku: {
    name: postgresSku
    tier: 'GeneralPurpose'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: postgresStorageGB
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

// ── PostgreSQL Firewall: Allow Azure Services ─────────────────────────
resource postgresFirewallAzure 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgresServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

// ── PostgreSQL Database ───────────────────────────────────────────────
resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgresServer
  name: 'tasksdb'
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// ── Alert: High Error Rate ────────────────────────────────────────────
resource alertHighErrorRate 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${baseName}-high-error-rate'
  location: 'global'
  properties: {
    severity: 1
    enabled: true
    scopes: [appService.id]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'Http5xxRate'
          metricName: 'Http5xx'
          operator: 'GreaterThan'
          threshold: 5
          timeAggregation: 'Total'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    description: 'Alert when 5xx error count exceeds 5 in a 5-minute window'
  }
}

// ── Alert: Slow Response Time ─────────────────────────────────────────
resource alertSlowResponse 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: '${baseName}-slow-response'
  location: 'global'
  properties: {
    severity: 2
    enabled: true
    scopes: [appService.id]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'HighResponseTime'
          metricName: 'HttpResponseTime'
          operator: 'GreaterThan'
          threshold: 3
          timeAggregation: 'Average'
          criterionType: 'StaticThresholdCriterion'
        }
      ]
    }
    description: 'Alert when average response time exceeds 3 seconds'
  }
}

// ── Outputs ───────────────────────────────────────────────────────────
output appServiceUrl string = 'https://${appService.properties.defaultHostName}'
output appServiceName string = appService.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
