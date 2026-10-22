param profileName string
param profileSku string
param endpointName string
param originHost string
param tags object

resource profile 'Microsoft.Cdn/profiles@2025-06-01' existing = {
  name: profileName
}

module waf 'br/public:avm/res/network/front-door-web-application-firewall-policy:0.3.3' = {
  name: 'edge-waf-policy'
  params: {
    name: replace('${profileName}waf', '-', '')
    sku: profileSku
    policySettings: {
      enabledState: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: 'Enabled'
    }
    customRules: {
      rules: [
        {
          name: 'ApiRateLimit'
          priority: 10
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: 60
          action: 'Block'
          matchConditions: [
            { matchVariable: 'RequestUri', operator: 'Contains', matchValue: ['/api/'], transforms: ['Lowercase'] }
          ]
        }
      ]
    }
    tags: tags
    enableTelemetry: false
  }
}

// Child resources are separated from the AVM profile to avoid a profile/site dependency cycle.
resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2025-06-01' = {
  parent: profile
  name: endpointName
  location: 'global'
  properties: { enabledState: 'Enabled' }
}

resource origins 'Microsoft.Cdn/profiles/originGroups@2025-06-01' = {
  parent: profile
  name: 'europe'
  properties: {
    sessionAffinityState: 'Disabled'
    healthProbeSettings: {
      probePath: '/healthz'
      probeRequestType: 'HEAD'
      probeProtocol: 'Https'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 50
    }
  }
}

resource origin 'Microsoft.Cdn/profiles/originGroups/origins@2025-06-01' = {
  parent: origins
  name: 'appservice'
  properties: {
    hostName: originHost
    originHostHeader: originHost
    httpPort: 80
    httpsPort: 443
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
    priority: 1
    weight: 1000
  }
}

resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2025-06-01' = {
  parent: endpoint
  name: 'application'
  properties: {
    enabledState: 'Enabled'
    originGroup: { id: origins.id }
    supportedProtocols: ['Http', 'Https']
    patternsToMatch: ['/*']
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Enabled'
    // No edge caching: authentication cookies and personal responses must never be shared.
  }
  dependsOn: [origin]
}

resource security 'Microsoft.Cdn/profiles/securityPolicies@2025-06-01' = {
  parent: profile
  name: 'application-waf'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: { id: waf.outputs.resourceId }
      associations: [
        { domains: [{ id: endpoint.id }], patternsToMatch: ['/*'] }
      ]
    }
  }
}

output hostname string = endpoint.properties.hostName
