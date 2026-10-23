using './main.bicep'

param location = 'swedencentral'
param workloadName = 'skycast'
param environmentName = 'prod'
param tenantId = readEnvironmentVariable('AZURE_TENANT_ID')
param entraClientId = readEnvironmentVariable('ENTRA_CLIENT_ID')
param entraClientSecret = readEnvironmentVariable('ENTRA_CLIENT_SECRET')
param openWeatherApiKey = readEnvironmentVariable('OPENWEATHER_API_KEY')
param databaseAdminGroupObjectId = readEnvironmentVariable('DATABASE_ADMIN_GROUP_OBJECT_ID')
param databaseAdminGroupName = readEnvironmentVariable('DATABASE_ADMIN_GROUP_NAME')
param alertEmail = readEnvironmentVariable('ALERT_EMAIL')
