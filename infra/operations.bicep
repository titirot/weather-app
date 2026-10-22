param prefix string
param location string
param planId string
param siteId string
param databaseId string
param minimumInstances int
param alertEmail string
param tags object

module notifications 'br/public:avm/res/insights/action-group:0.8.0' = {
  name: 'operational-notifications'
  params: {
    name: '${prefix}-oncall'
    groupShortName: 'SkyCast'
    emailReceivers: [
      { name: 'oncall', emailAddress: alertEmail, useCommonAlertSchema: true }
    ]
    tags: tags
    enableTelemetry: false
  }
}

resource autoscale 'Microsoft.Insights/autoscaleSettings@2022-10-01' = {
  name: '${prefix}-web-autoscale'
  location: location
  tags: tags
  properties: {
    enabled: true
    targetResourceUri: planId
    profiles: [
      {
        name: 'zone-resilient'
        capacity: {
          minimum: string(minimumInstances)
          default: string(minimumInstances)
          maximum: string(minimumInstances + 2)
        }
        rules: [
          {
            metricTrigger: {
              metricName: 'CpuPercentage'
              metricNamespace: 'Microsoft.Web/serverfarms'
              metricResourceUri: planId
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT10M'
              timeAggregation: 'Average'
              operator: 'GreaterThan'
              threshold: 65
            }
            scaleAction: { direction: 'Increase', type: 'ChangeCount', value: '1', cooldown: 'PT5M' }
          }
          {
            metricTrigger: {
              metricName: 'CpuPercentage'
              metricNamespace: 'Microsoft.Web/serverfarms'
              metricResourceUri: planId
              timeGrain: 'PT1M'
              statistic: 'Average'
              timeWindow: 'PT15M'
              timeAggregation: 'Average'
              operator: 'LessThan'
              threshold: 30
            }
            scaleAction: { direction: 'Decrease', type: 'ChangeCount', value: '1', cooldown: 'PT10M' }
          }
        ]
      }
    ]
    notifications: [
      { operation: 'Scale', email: { customEmails: [alertEmail], sendToSubscriptionAdministrator: false, sendToSubscriptionCoAdministrators: false } }
    ]
  }
}

var alertDefinitions = [
  {
    name: 'web-errors'
    scope: siteId
    namespace: 'Microsoft.Web/sites'
    metric: 'Http5xx'
    aggregation: 'Total'
    threshold: 5
    description: 'More than five HTTP 5xx responses in five minutes. Check app and database connectivity.'
  }
  {
    name: 'database-cpu'
    scope: databaseId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'cpu_percent'
    aggregation: 'Average'
    threshold: 80
    description: 'Sustained database CPU pressure; investigate queries and pool usage before scaling.'
  }
  {
    name: 'database-storage'
    scope: databaseId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'storage_percent'
    aggregation: 'Maximum'
    threshold: 80
    description: 'Database storage is more than 80 percent used. Auto-grow is enabled; investigate growth and cost.'
  }
]

module alerts 'br/public:avm/res/insights/metric-alert:0.4.1' = [for definition in alertDefinitions: {
  name: 'alert-${definition.name}'
  params: {
    name: '${prefix}-${definition.name}'
    severity: 2
    alertDescription: definition.description
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    scopes: [definition.scope]
    actions: [notifications.outputs.resourceId]
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allof: [
        {
          name: definition.name
          criterionType: 'StaticThresholdCriterion'
          metricName: definition.metric
          metricNamespace: definition.namespace
          timeAggregation: definition.aggregation
          operator: 'GreaterThan'
          threshold: definition.threshold
        }
      ]
    }
    tags: tags
    enableTelemetry: false
  }
}]
