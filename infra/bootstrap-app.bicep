targetScope = 'resourceGroup'

param name string
param location string
param planId string
param subnetId string
param identityResourceId string
param identityClientId string
param databaseHost string
param databaseUser string
param runtimeRole string
param runtimeObjectId string
param runnerIpv4 string

@secure()
param statusToken string

// Temporary release-only app: shares paid capacity, never the customer's identity.
module site 'br/public:avm/res/web/site:0.24.0' = {
  name: 'temporary-bootstrap-app'
  params: {
    name: name
    location: location
    kind: 'app,linux'
    serverFarmResourceId: planId
    httpsOnly: true
    clientAffinityEnabled: false
    virtualNetworkSubnetResourceId: subnetId
    publicNetworkAccess: 'Enabled'
    managedIdentities: { userAssignedResourceIds: [identityResourceId] }
    outboundVnetRouting: { allTraffic: true }
    basicPublishingCredentialsPolicies: [
      { name: 'ftp', allow: false }
      { name: 'scm', allow: false }
    ]
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.12'
      appCommandLine: 'gunicorn --bind=0.0.0.0:8000 --workers=1 --threads=2 --timeout=300 "bootstrap_app:create_app()"'
      alwaysOn: false
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ipSecurityRestrictionsDefaultAction: 'Deny'
      ipSecurityRestrictions: [
        { name: 'ReleaseStatus', priority: 100, action: 'Allow', ipAddress: '${runnerIpv4}/32' }
      ]
      scmIpSecurityRestrictionsUseMain: false
      scmIpSecurityRestrictionsDefaultAction: 'Deny'
      scmIpSecurityRestrictions: [
        { name: 'ReleaseUpload', priority: 100, action: 'Allow', ipAddress: '${runnerIpv4}/32' }
      ]
      appSettings: [
        { name: 'SCM_DO_BUILD_DURING_DEPLOYMENT', value: 'true' }
        { name: 'AZURE_CLIENT_ID', value: identityClientId }
        { name: 'PGHOST', value: databaseHost }
        { name: 'PGUSER', value: databaseUser }
        { name: 'RUNTIME_ROLE', value: runtimeRole }
        { name: 'RUNTIME_OBJECT_ID', value: runtimeObjectId }
        { name: 'BOOTSTRAP_STATUS_TOKEN', value: statusToken }
      ]
    }
    tags: { managedBy: 'Bicep', purpose: 'temporary-database-bootstrap' }
    enableTelemetry: false
  }
}
