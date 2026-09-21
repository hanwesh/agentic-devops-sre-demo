targetScope = 'resourceGroup'

@description('Globally unique app/server prefix. Use a dedicated disposable resource group.')
@minLength(3)
@maxLength(24)
param baseName string

param location string = resourceGroup().location

@description('Bootstrap administrator only; NEVER used by application or migration jobs.')
param postgresAdminLogin string = 'bootstrapadmin'

@secure()
param postgresAdminPassword string

param postgresSku string = 'Standard_D2s_v3'

@minValue(32)
param postgresStorageGB int = 32

@description('Distinct logical databases. Bootstrap and protected jobs must use these exact names.')
@minLength(1)
@maxLength(63)
param productionDatabaseName string = 'taskdb_production'
@minLength(1)
@maxLength(63)
param stagingDatabaseName string = 'taskdb_staging'
@minLength(1)
@maxLength(63)
param demoDatabaseName string = 'taskdb_demo'

@description('Existing versioned Key Vault secret URI for the production runtime role.')
param productionDatabaseSecretUri string

@description('Existing versioned Key Vault secret URI for the staging runtime role.')
param stagingDatabaseSecretUri string

@description('Required when enableDemoSlot=true. Never reuse either other database secret.')
param demoDatabaseSecretUri string = ''

@description('Opt in to a separate demo database, telemetry workspace, and app slot.')
param enableDemoSlot bool = false

@description('Enable only after demo telemetry has arrived and the receiver is verified.')
param enableDemoAlerts bool = false

@description('Operator mailbox. SRE incident-platform binding is a separate operator step.')
param alertEmailAddress string = ''

@description('Opt-in PAID service. Requires enableDemoSlot and reviewed setup prerequisites.')
param enableSreAgent bool = false

@allowed([
  'australiaeast'
  'eastus2'
  'swedencentral'
])
param sreAgentLocation string = 'eastus2'

@description('Use a model/provider offered for this subscription and region; no invented default.')
param sreAgentModelName string = ''
param sreAgentModelProvider string = ''

@description('Change/ticket reference only, NOT an ARM consent property or an OAuth token.')
param sreAgentConsentReference string = ''

@description('Separate opt-in: grant only Reader/Monitoring Reader/Log Analytics Reader on demo resources.')
param grantSreInvestigationRoles bool = false

@description('Choose nonoverlapping CIDRs after reviewing existing VNet/runner connectivity.')
param vnetAddressPrefix string = '10.42.0.0/16'
param appSubnetPrefix string = '10.42.1.0/24'
param postgresSubnetPrefix string = '10.42.2.0/24'

var environments = enableDemoSlot ? ['production', 'staging', 'demo'] : ['production', 'staging']
var slotEnvironments = enableDemoSlot ? ['staging', 'demo'] : ['staging']
var databaseNames = {
  production: productionDatabaseName
  staging: stagingDatabaseName
  demo: demoDatabaseName
}
var secretUris = {
  production: productionDatabaseSecretUri
  staging: stagingDatabaseSecretUri
  demo: demoDatabaseSecretUri
}
var tags = {
  purpose: 'agentic-devops-sre-demo'
  deployment: resourceGroup().name
}
var commonSettings = [
  {
    name: 'DATABASE_SSL_REQUIRED'
    value: 'true'
  }
  {
    name: 'DEMO_SCENARIO_ENABLED'
    value: 'false'
  }
  {
    name: 'DEMO_RUN_ID'
    value: ''
  }
  {
    name: 'SCM_DO_BUILD_DURING_DEPLOYMENT'
    value: 'true'
  }
  {
    name: 'OTEL_SERVICE_NAME'
    value: 'task-api'
  }
  {
    name: 'OTEL_TRACES_SAMPLER'
    value: 'always_on'
  }
  {
    name: 'WEBSITE_SWAP_WARMUP_PING_PATH'
    value: '/live'
  }
  {
    name: 'WEBSITE_SWAP_WARMUP_PING_STATUSES'
    value: '200'
  }
  {
    name: 'WEBSITE_WARMUP_PATH'
    value: '/live'
  }
  {
    name: 'WEBSITE_WARMUP_STATUSES'
    value: '200'
  }
]

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${baseName}-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [vnetAddressPrefix]
    }
    subnets: [
      {
        name: 'apps'
        properties: {
          addressPrefix: appSubnetPrefix
          delegations: [
            {
              name: 'app-service'
              properties: {
                serviceName: 'Microsoft.Web/serverFarms'
              }
            }
          ]
          serviceEndpoints: [
            {
              service: 'Microsoft.KeyVault'
            }
          ]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: postgresSubnetPrefix
          delegations: [
            {
              name: 'postgresql'
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

resource privateDns 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: '${baseName}.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: privateDns
  name: 'app-database'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: '${baseName}-pg'
  location: location
  tags: tags
  sku: {
    name: postgresSku
    tier: 'GeneralPurpose'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminLogin
    administratorLoginPassword: postgresAdminPassword
    authConfig: {
      activeDirectoryAuth: 'Disabled'
      passwordAuth: 'Enabled'
    }
    network: {
      delegatedSubnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'postgres')
      privateDnsZoneArmResourceId: privateDns.id
      publicNetworkAccess: 'Disabled'
    }
    storage: {
      storageSizeGB: postgresStorageGB
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
  dependsOn: [dnsLink]
}

resource databases 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = [for environment in environments: {
  parent: postgresServer
  name: databaseNames[environment]
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}]

// Separate workspaces keep demo log-reader access away from production telemetry.
resource workspaces 'Microsoft.OperationalInsights/workspaces@2023-09-01' = [for environment in environments: {
  name: '${baseName}-${environment}-logs'
  location: location
  tags: union(tags, { environment: environment })
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}]

resource insights 'Microsoft.Insights/components@2020-02-02' = [for (environment, i) in environments: {
  name: '${baseName}-${environment}-insights'
  location: location
  kind: 'web'
  tags: union(tags, { environment: environment })
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaces[i].id
    SamplingPercentage: 100
    IngestionMode: 'LogAnalytics'
  }
}]

resource plan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${baseName}-plan'
  location: location
  kind: 'linux'
  tags: tags
  sku: {
    name: 'S1'
    tier: 'Standard'
    capacity: 1
  }
  properties: {
    reserved: true
  }
}

resource app 'Microsoft.Web/sites@2023-12-01' = {
  name: baseName
  location: location
  kind: 'app,linux'
  tags: union(tags, { environment: 'production' })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    virtualNetworkSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'apps')
    vnetRouteAllEnabled: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'uvicorn src.main:app --host 0.0.0.0 --port 8000'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      healthCheckPath: '/ready'
      appSettings: concat(commonSettings, [
        {
          name: 'DATABASE_URL'
          value: '@Microsoft.KeyVault(SecretUri=${productionDatabaseSecretUri})'
        }
        {
          name: 'ENVIRONMENT'
          value: 'production'
        }
        {
          name: 'DATABASE_NAME'
          value: productionDatabaseName
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights[0].properties.ConnectionString
        }
      ])
    }
  }
}

resource slots 'Microsoft.Web/sites/slots@2023-12-01' = [for (environment, i) in slotEnvironments: {
  parent: app
  name: environment
  location: location
  kind: 'app,linux'
  tags: union(tags, { environment: environment })
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    clientAffinityEnabled: false
    virtualNetworkSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnet.name, 'apps')
    vnetRouteAllEnabled: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'uvicorn src.main:app --host 0.0.0.0 --port 8000'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      healthCheckPath: '/ready'
      appSettings: concat(commonSettings, [
        {
          name: 'DATABASE_URL'
          value: '@Microsoft.KeyVault(SecretUri=${secretUris[environment]})'
        }
        {
          name: 'ENVIRONMENT'
          value: environment
        }
        {
          name: 'DATABASE_NAME'
          value: databaseNames[environment]
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: insights[i + 1].properties.ConnectionString
        }
      ])
    }
  }
}]

resource stickySettings 'Microsoft.Web/sites/config@2023-12-01' = {
  parent: app
  name: 'slotConfigNames'
  properties: {
    appSettingNames: [
      'ENVIRONMENT'
      'DATABASE_URL'
      'DATABASE_NAME'
      'DATABASE_SSL_REQUIRED'
      'APPLICATIONINSIGHTS_CONNECTION_STRING'
      'OTEL_SERVICE_NAME'
      'OTEL_TRACES_SAMPLER'
      'DEMO_SCENARIO_ENABLED'
      'DEMO_RUN_ID'
    ]
  }
}

module demoAlerts './alerts.bicep' = if (enableDemoSlot) {
  name: 'demo-alerts'
  params: {
    baseName: baseName
    location: location
    enabled: enableDemoAlerts
    alertEmailAddress: alertEmailAddress
    workspaceResourceId: resourceId('Microsoft.OperationalInsights/workspaces', '${baseName}-demo-logs')
    insightsResourceId: resourceId('Microsoft.Insights/components', '${baseName}-demo-insights')
    demoAppResourceId: resourceId('Microsoft.Web/sites/slots', baseName, 'demo')
    tags: tags
  }
  dependsOn: [workspaces, insights, slots]
}

module sreAgent './sre-agent.bicep' = if (enableSreAgent && enableDemoSlot) {
  name: 'sre-agent'
  params: {
    name: '${baseName}-sre'
    location: sreAgentLocation
    modelName: sreAgentModelName
    modelProvider: sreAgentModelProvider
    consentReference: sreAgentConsentReference
    grantInvestigationRoles: grantSreInvestigationRoles
    appName: app.name
    demoWorkspaceName: '${baseName}-demo-logs'
    demoInsightsName: '${baseName}-demo-insights'
    tags: tags
  }
  dependsOn: [workspaces, insights, slots]
}

output appServiceName string = app.name
output appServiceUrl string = 'https://${app.properties.defaultHostName}'
output postgresServerFqdn string = postgresServer.properties.fullyQualifiedDomainName
output privateNetworkId string = vnet.id
output environmentResources array = [for (environment, i) in environments: {
  environment: environment
  database: databaseNames[environment]
  insightsResourceId: insights[i].id
  workspaceResourceId: workspaces[i].id
  workspaceCustomerId: workspaces[i].properties.customerId
}]
output productionIdentityPrincipalId string = app.identity.principalId
output slotIdentities array = [for (environment, i) in slotEnvironments: {
  environment: environment
  principalId: slots[i].identity.principalId
}]
