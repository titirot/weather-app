param prefix string
param location string
param tags object
param webSku string
param minimumInstances int
param subnetId string
param identityResourceId string
param identityClientId string
param databaseHost string
param databaseUser string
param vaultName string
param workspaceId string
param frontDoorProfileName string
param tenantId string
param entraClientId string

// Resolve only inside this module, after the parent has created the profile.
resource edgeProfile 'Microsoft.Cdn/profiles@2025-06-01' existing = {
  name: frontDoorProfileName
}

module plan 'br/public:avm/res/web/serverfarm:0.7.0' = {
  name: 'zone-redundant-plan'
  params: {
    name: '${prefix}-plan'
    location: location
    kind: 'linux'
    reserved: true
    skuName: webSku
    skuCapacity: minimumInstances
    zoneRedundant: true
    tags: tags
    enableTelemetry: false
  }
}

module site 'br/public:avm/res/web/site:0.24.0' = {
  name: 'web-site'
  params: {
    name: '${prefix}-web'
    location: location
    kind: 'app,linux'
    serverFarmResourceId: plan.outputs.resourceId
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    virtualNetworkSubnetResourceId: subnetId
    managedIdentities: { userAssignedResourceIds: [identityResourceId] }
    keyVaultAccessIdentityResourceId: identityResourceId
    outboundVnetRouting: { allTraffic: true }
    basicPublishingCredentialsPolicies: [
      { name: 'ftp', allow: false }
      { name: 'scm', allow: false }
    ]
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 --workers=2 --threads=4 --timeout=60 app:app'
      alwaysOn: true
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      http20Enabled: true
      healthCheckPath: '/healthz'
      ipSecurityRestrictionsDefaultAction: 'Deny'
      ipSecurityRestrictions: [
        {
          name: 'OnlyThisFrontDoor'
          priority: 100
          action: 'Allow'
          tag: 'ServiceTag'
          ipAddress: 'AzureFrontDoor.Backend'
          headers: { 'x-azure-fdid': [edgeProfile.properties.frontDoorId] }
        }
      ]
      scmIpSecurityRestrictionsUseMain: false
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: []
      appSettings: [
        { name: 'AUTH_MODE', value: 'easyauth' }
        { name: 'AZURE_CLIENT_ID', value: identityClientId }
        { name: 'PG_AUTH', value: 'entra' }
        { name: 'PGHOST', value: databaseHost }
        { name: 'PGPORT', value: '5432' }
        { name: 'PGDATABASE', value: 'weather' }
        { name: 'PGUSER', value: databaseUser }
        { name: 'PGSSLMODE', value: 'verify-full' }
        { name: 'PGSSLROOTCERT', value: '/etc/ssl/certs/ca-certificates.crt' }
        { name: 'PG_POOL_MAX_SIZE', value: '4' }
        { name: 'WEATHER_CACHE_SECONDS', value: '600' }
        { name: 'ACTIVITY_RETENTION_DAYS', value: '90' }
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'WEBSITE_HEALTHCHECK_MAXPINGFAILURES', value: '3' }
        { name: 'OPENWEATHER_API_KEY', value: '@Microsoft.KeyVault(VaultName=${vaultName};SecretName=openweather-api-key)' }
        { name: 'ENTRA_CLIENT_SECRET', value: '@Microsoft.KeyVault(VaultName=${vaultName};SecretName=entra-client-secret)' }
      ]
    }
    diagnosticSettings: [
      {
        workspaceResourceId: workspaceId
        metricCategories: [{ category: 'AllMetrics' }]
        logCategoriesAndGroups: [
          { category: 'AppServicePlatformLogs' }
        ]
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

resource webApp 'Microsoft.Web/sites@2025-03-01' existing = {
  name: '${prefix}-web'
}

// Easy Auth is a child configuration; no independent AVM exists for this setting.
resource authentication 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: webApp
  name: 'authsettingsV2'
  properties: {
    platform: { enabled: true, runtimeVersion: '~1' }
    globalValidation: { requireAuthentication: false, unauthenticatedClientAction: 'AllowAnonymous' }
    httpSettings: {
      requireHttps: true
      forwardProxy: { convention: 'Standard' }
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'ENTRA_CLIENT_SECRET'
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
        }
        validation: { allowedAudiences: [entraClientId, 'api://${entraClientId}'] }
      }
    }
    login: {
      tokenStore: { enabled: false }
      preserveUrlFragmentsForLogins: false
    }
  }
  dependsOn: [site]
}

resource privacyLogs 'Microsoft.Web/sites/config@2025-03-01' = {
  parent: webApp
  name: 'logs'
  properties: {
    httpLogs: { fileSystem: { enabled: false } }
    detailedErrorMessages: { enabled: false }
    failedRequestsTracing: { enabled: false }
    applicationLogs: { fileSystem: { level: 'Off' } }
  }
  dependsOn: [site]
}

output siteId string = site.outputs.resourceId
output planId string = plan.outputs.resourceId
output name string = site.outputs.name
output defaultHostname string = webApp.properties.defaultHostName
