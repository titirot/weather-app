param prefix string
param location string
param tags object

module outbound 'br/public:avm/res/network/nat-gateway:2.1.1' = {
  name: 'zone-resilient-egress'
  params: {
    name: '${prefix}-nat'
    location: location
    availabilityZone: -1
    natGatewaySku: 'StandardV2'
    publicIPAddresses: [{ name: '${prefix}-egress', skuName: 'StandardV2' }]
    tags: tags
    enableTelemetry: false
  }
}

module databaseNsg 'br/public:avm/res/network/network-security-group:0.5.4' = {
  name: 'database-network-boundary'
  params: {
    name: '${prefix}-db-nsg'
    location: location
    securityRules: [
      {
        name: 'PostgresFromApplicationAndHA'
        properties: {
          priority: 100
          direction: 'Inbound'
          access: 'Allow'
          protocol: 'Tcp'
          sourceAddressPrefixes: ['10.42.0.0/26', '10.42.1.0/27', '10.42.3.0/27']
          sourcePortRange: '*'
          destinationAddressPrefix: '10.42.1.0/27'
          destinationPortRange: '5432'
        }
      }
      {
        name: 'DenyOtherInbound'
        properties: {
          priority: 200
          direction: 'Inbound'
          access: 'Deny'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'workload-vnet'
  params: {
    name: '${prefix}-vnet'
    location: location
    addressPrefixes: ['10.42.0.0/16']
    subnets: [
      {
        name: 'app-integration'
        addressPrefix: '10.42.0.0/26'
        delegation: 'Microsoft.Web/serverFarms'
        natGatewayResourceId: outbound.outputs.resourceId
        defaultOutboundAccess: false
      }
      {
        name: 'postgresql'
        addressPrefix: '10.42.1.0/27'
        delegation: 'Microsoft.DBforPostgreSQL/flexibleServers'
        networkSecurityGroupResourceId: databaseNsg.outputs.resourceId
        natGatewayResourceId: outbound.outputs.resourceId
        defaultOutboundAccess: false
      }
      {
        name: 'private-endpoints'
        addressPrefix: '10.42.2.0/27'
        privateEndpointNetworkPolicies: 'Disabled'
        defaultOutboundAccess: false
      }
      {
        name: 'administration'
        addressPrefix: '10.42.3.0/27'
        natGatewayResourceId: outbound.outputs.resourceId
        defaultOutboundAccess: false
      }
    ]
    tags: tags
    enableTelemetry: false
  }
}

module postgresDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'postgres-private-dns'
  params: {
    name: '${prefix}.postgres.database.azure.com'
    virtualNetworkLinks: [
      { name: 'workload', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
    tags: tags
    enableTelemetry: false
  }
}

module vaultDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'vault-private-dns'
  params: {
    name: 'privatelink.vaultcore.azure.net'
    virtualNetworkLinks: [
      { name: 'workload', virtualNetworkResourceId: vnet.outputs.resourceId, registrationEnabled: false }
    ]
    tags: tags
    enableTelemetry: false
  }
}

output appSubnetId string = '${vnet.outputs.resourceId}/subnets/app-integration'
output administrationSubnetId string = '${vnet.outputs.resourceId}/subnets/administration'
output natGatewayId string = outbound.outputs.resourceId
output databaseSubnetId string = '${vnet.outputs.resourceId}/subnets/postgresql'
output privateEndpointSubnetId string = '${vnet.outputs.resourceId}/subnets/private-endpoints'
output databaseDnsZoneId string = postgresDns.outputs.resourceId
output vaultDnsZoneId string = vaultDns.outputs.resourceId
