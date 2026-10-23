targetScope = 'resourceGroup'

@description('Must match the subsequent main.bicep deployment.')
@allowed(['swedencentral', 'northeurope', 'westeurope'])
param location string = 'swedencentral'

@minLength(3)
@maxLength(12)
param workloadName string = 'skycast'

@allowed(['prod', 'test'])
param environmentName string = 'prod'

var suffix = uniqueString(subscription().subscriptionId, resourceGroup().id, workloadName, environmentName)
var prefix = '${workloadName}-${environmentName}-${suffix}'

// Reuse the production module so the later deployment preserves subnet and egress IDs.
module network 'network.bicep' = {
  name: 'private-network'
  params: {
    prefix: prefix
    location: location
    tags: {
      workload: workloadName
      environment: environmentName
      managedBy: 'Bicep'
      dataClassification: 'personal'
    }
  }
}

output releasePublicIpName string = '${prefix}-egress'
output administrationSubnetId string = network.outputs.administrationSubnetId
output natGatewayId string = network.outputs.natGatewayId
