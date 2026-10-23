targetScope = 'resourceGroup'

@description('European origin region. Confirm regional SKU capacity before deployment.')
@allowed(['swedencentral', 'northeurope', 'westeurope'])
param location string = 'swedencentral'

@description('Short lowercase workload name used for resource names.')
@minLength(3)
@maxLength(12)
param workloadName string = 'skycast'

@allowed(['prod', 'test'])
param environmentName string = 'prod'

@description('Entra tenant containing the application registration and database administrators.')
param tenantId string = subscription().tenantId

@description('Application/client ID of the existing Entra web application registration.')
param entraClientId string

@secure()
@description('Client secret VALUE for Easy Auth. Stored in Key Vault, never output.')
param entraClientSecret string

@secure()
@description('OpenWeather API key. Stored in Key Vault, never output.')
param openWeatherApiKey string

@description('Object ID of an existing Entra security group for human database administrators, NOT the runtime identity.')
param databaseAdminGroupObjectId string

@description('Display name of that Entra group; used as the PostgreSQL administrator login.')
param databaseAdminGroupName string

@description('Email receiving operational alerts. Required so production alerts have a destination.')
param alertEmail string

@description('Premium includes managed WAF rules. Standard is an explicit security/cost downgrade.')
@allowed(['Premium_AzureFrontDoor', 'Standard_AzureFrontDoor'])
param frontDoorSku string = 'Premium_AzureFrontDoor'

@description('Start at two zone-distributed workers; autoscale never scales below two.')
@minValue(2)
@maxValue(10)
param minimumWebInstances int = 2

@description('Compute sizes remain configurable for load testing and quota availability.')
@allowed(['P0v3', 'P1v3', 'P2v3'])
param webSku string = 'P0v3'

@allowed(['Standard_D2ds_v5', 'Standard_D4ds_v5'])
param databaseSku string = 'Standard_D2ds_v5'

@description('Geo-backup is selected at server creation. Confirm availability in the target region.')
param geoRedundantBackup bool = true

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, workloadName, environmentName)
var prefix = '${workloadName}-${environmentName}-${suffix}'
var tags = {
  workload: workloadName
  environment: environmentName
  managedBy: 'Bicep'
  dataClassification: 'personal'
}

module runtimeIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'runtime-identity'
  params: {
    name: '${prefix}-app'
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module bootstrapIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'bootstrap-identity'
  params: {
    name: '${prefix}-bootstrap'
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module workspace 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'monitoring-workspace'
  params: {
    name: '${prefix}-logs'
    location: location
    dataRetention: 30
    dailyQuotaGb: '1'
    forceCmkForQuery: false
    tags: tags
    enableTelemetry: false
  }
}

module network 'network.bicep' = {
  name: 'private-network'
  params: {
    prefix: prefix
    location: location
    tags: tags
  }
}

module vault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: 'application-secrets'
  params: {
    name: 'kv-${suffix}'
    location: location
    sku: 'standard'
    enableRbacAuthorization: true
    enablePurgeProtection: true
    softDeleteRetentionInDays: 90
    enableVaultForDeployment: false
    enableVaultForTemplateDeployment: false
    enableVaultForDiskEncryption: false
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
    }
    secrets: [
      { name: 'openweather-api-key', value: openWeatherApiKey }
      { name: 'entra-client-secret', value: entraClientSecret }
    ]
    privateEndpoints: [
      {
        name: '${prefix}-vault-pe'
        subnetResourceId: network.outputs.privateEndpointSubnetId
        privateDnsZoneGroup: {
          privateDnsZoneGroupConfigs: [
            { privateDnsZoneResourceId: network.outputs.vaultDnsZoneId }
          ]
        }
      }
    ]
    roleAssignments: [
      {
        principalId: runtimeIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'Key Vault Secrets User'
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

// Pin the most recent inspected release using the stable 2024-08-01 server API.
module database 'br/public:avm/res/db-for-postgre-sql/flexible-server:0.13.0' = {
  name: 'postgresql'
  params: {
    name: '${prefix}-pg'
    location: location
    version: '17'
    skuName: databaseSku
    tier: 'GeneralPurpose'
    availabilityZone: 1
    highAvailabilityZone: 2
    highAvailability: 'ZoneRedundant'
    backupRetentionDays: 14
    geoRedundantBackup: geoRedundantBackup ? 'Enabled' : 'Disabled'
    storageSizeGB: 32
    autoGrow: 'Enabled'
    tenantId: tenantId
    administrators: [
      {
        objectId: databaseAdminGroupObjectId
        principalName: databaseAdminGroupName
        principalType: 'Group'
        tenantId: tenantId
      }
    ]
    publicNetworkAccess: 'Disabled'
    delegatedSubnetResourceId: network.outputs.databaseSubnetId
    privateDnsZoneArmResourceId: network.outputs.databaseDnsZoneId
    databases: [
      { name: 'weather', charset: 'UTF8', collation: 'en_US.utf8' }
    ]
    configurations: [
      { name: 'require_secure_transport', value: 'on', source: 'user-override' }
      { name: 'log_statement', value: 'none', source: 'user-override' }
      { name: 'log_min_error_statement', value: 'panic', source: 'user-override' }
    ]
    // Opt into Defender separately after confirming its recurring price.
    enableAdvancedThreatProtection: false
    diagnosticSettings: [
      {
        workspaceResourceId: workspace.outputs.resourceId
        metricCategories: [{ category: 'AllMetrics' }]
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

// The profile is created before the site so ingress can match its real Front Door ID.
module frontDoor 'br/public:avm/res/cdn/profile:0.20.0' = {
  name: 'global-edge-profile'
  params: {
    name: '${prefix}-fd'
    location: 'global'
    sku: frontDoorSku
    originResponseTimeoutSeconds: 60
    tags: tags
    diagnosticSettings: [
      {
        workspaceResourceId: workspace.outputs.resourceId
        metricCategories: [{ category: 'AllMetrics' }]
      }
    ]
    enableTelemetry: false
  }
}

module web 'web.bicep' = {
  name: 'flask-web-application'
  params: {
    prefix: prefix
    location: location
    tags: tags
    webSku: webSku
    minimumInstances: minimumWebInstances
    subnetId: network.outputs.appSubnetId
    identityResourceId: runtimeIdentity.outputs.resourceId
    identityClientId: runtimeIdentity.outputs.clientId
    databaseHost: database.outputs.?fqdn!
    databaseUser: runtimeIdentity.outputs.name
    vaultName: vault.outputs.name
    workspaceId: workspace.outputs.resourceId
    frontDoorProfileName: frontDoor.outputs.name
    tenantId: tenantId
    entraClientId: entraClientId
  }
}

module edge 'edge.bicep' = {
  name: 'edge-routing-and-waf'
  params: {
    profileName: frontDoor.outputs.name
    profileSku: frontDoorSku
    endpointName: '${prefix}-edge'
    originHost: web.outputs.defaultHostname
    tags: tags
  }
}

module operations 'operations.bicep' = {
  name: 'scaling-and-alerts'
  params: {
    prefix: prefix
    location: location
    planId: web.outputs.planId
    siteId: web.outputs.siteId
    databaseId: database.outputs.resourceId
    minimumInstances: minimumWebInstances
    alertEmail: alertEmail
    tags: tags
  }
}

output applicationUrl string = 'https://${edge.outputs.hostname}'
output entraRedirectUri string = 'https://${edge.outputs.hostname}/.auth/login/aad/callback'
output webAppName string = web.outputs.name
output postgresHost string = database.outputs.?fqdn!
output postgresAdminLogin string = databaseAdminGroupName
output postgresServerName string = database.outputs.name
output bootstrapIdentityResourceId string = bootstrapIdentity.outputs.resourceId
output bootstrapIdentityClientId string = bootstrapIdentity.outputs.clientId
output bootstrapIdentityObjectId string = bootstrapIdentity.outputs.principalId
output bootstrapIdentityName string = bootstrapIdentity.outputs.name
output bootstrapWebAppName string = '${prefix}-bootstrap-web'
output appServicePlanId string = web.outputs.planId
output appSubnetId string = network.outputs.appSubnetId
output deploymentLocation string = location
output runtimeDatabaseRole string = runtimeIdentity.outputs.name
output runtimePrincipalObjectId string = runtimeIdentity.outputs.principalId
output keyVaultName string = vault.outputs.name
output workspaceName string = workspace.outputs.name
