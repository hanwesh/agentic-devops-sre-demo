param baseName string
param location string
param workspaceResourceId string
param insightsResourceId string
param demoAppResourceId string
param enabled bool = false
param alertEmailAddress string = ''
param tags object

var policy = loadJsonContent('./alerts/policy.json')
var querySource = loadTextContent('./alerts/request-window.kql')
var queryScope = replace(replace(replace(querySource,
  '__INSIGHTS_RESOURCE_ID__', insightsResourceId),
  '__APP_RESOURCE_ID__', demoAppResourceId),
  '__ENVIRONMENT__', policy.environment)
var querySampling = replace(replace(replace(queryScope,
  '__MINIMUM_SAMPLES__', string(policy.minimum_samples)),
  '__PERCENTILE__', string(policy.percentile)),
  '__WINDOW_MINUTES__', string(policy.window_minutes))
var requestQuery = replace(replace(querySampling,
  '__SERVICE_NAME__', policy.service_name),
  '__EXCLUDED_PATHS__', string(policy.excluded_paths))
var rules = [
  {
    signal: 'http-5xx'
    policy: policy.rules['http-5xx']
    query: '${requestQuery}${loadTextContent('./alerts/http-5xx.kql')}'
    description: 'Demo HTTP 5xx ratio >5% over 5m, at least 20 observed request samples.'
  }
  {
    signal: 'latency-p95'
    policy: policy.rules['latency-p95']
    query: '${requestQuery}${loadTextContent('./alerts/latency-p95.kql')}'
    description: 'Demo weighted p95 duration >3000ms over 5m, at least 20 observed request samples.'
  }
]

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${baseName}-demo-action-group'
  location: 'global'
  tags: tags
  properties: {
    enabled: true
    groupShortName: 'sre-demo'
    emailReceivers: empty(alertEmailAddress) ? [] : [
      {
        name: 'approved-operator'
        emailAddress: alertEmailAddress
        useCommonAlertSchema: true
      }
    ]
  }
}

resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for rule in rules: {
  name: '${baseName}-demo-${rule.signal}'
  location: location
  kind: 'LogAlert'
  tags: union(tags, { environment: 'demo' })
  properties: {
    displayName: 'task-api demo ${rule.signal}'
    description: rule.description
    enabled: enabled && !empty(alertEmailAddress)
    severity: rule.policy.severity
    scopes: [workspaceResourceId]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    autoMitigate: true
    // A brand-new workspace has no AppRequests table. Enable only after live query validation.
    skipQueryValidation: !enabled
    criteria: {
      allOf: [
        {
          query: rule.query
          metricMeasureColumn: rule.policy.metric_column
          resourceIdColumn: 'ResourceId'
          operator: 'GreaterThan'
          threshold: rule.policy.threshold
          timeAggregation: 'Maximum'
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
          dimensions: [for dimension in ['ServiceName', 'Environment', 'DeploymentCommit', 'DemoRunId', 'IncidentFingerprint']: {
            name: dimension
            operator: 'Include'
            values: ['*']
          }]
        }
      ]
    }
    actions: {
      actionGroups: [actionGroup.id]
      customProperties: {
        service: policy.service_name
        environment: policy.environment
        signal: rule.signal
        schemaVersion: '1'
      }
    }
  }
}]

output actionGroupId string = actionGroup.id
