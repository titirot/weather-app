"""Check the actual compiled ARM contract; run using infra/validate.ps1."""

import json
import os
import unittest
from pathlib import Path


def resources(template):
    entries = template.get("resources", {})
    return list(entries.values()) if isinstance(entries, dict) else entries


@unittest.skipUnless(os.environ.get("COMPILED_INFRA_TEMPLATE"), "Run infra/validate.ps1")
class InfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = json.loads(
            Path(os.environ["COMPILED_INFRA_TEMPLATE"]).read_text(encoding="utf-8")
        )
        cls.modules = {
            resource["name"]: resource
            for resource in resources(cls.template)
            if resource["type"] == "Microsoft.Resources/deployments"
        }

    def module_template(self, name):
        return self.modules[name]["properties"]["template"]

    @unittest.skipUnless(os.environ.get("COMPILED_NETWORK_BOOTSTRAP"), "Run infra/validate.ps1")
    def test_network_bootstrap_matches_production(self):
        bootstrap = json.loads(
            Path(os.environ["COMPILED_NETWORK_BOOTSTRAP"]).read_text(encoding="utf-8")
        )
        modules = [r for r in resources(bootstrap)
                   if r["type"] == "Microsoft.Resources/deployments"]
        self.assertEqual([r["name"] for r in modules], ["private-network"])
        network = modules[0]["properties"]
        self.assertEqual(network["template"], self.module_template("private-network"))
        self.assertEqual(network["parameters"]["prefix"],
                         self.module_parameters("private-network")["prefix"])
        for name in ("suffix", "prefix"):
            self.assertEqual(bootstrap["variables"][name], self.template["variables"][name])
        for name in ("location", "workloadName", "environmentName"):
            self.assertEqual(bootstrap["parameters"][name]["defaultValue"],
                             self.template["parameters"][name]["defaultValue"])
        self.assertIn("releasePublicIpName", bootstrap["outputs"])
        self.assertIn("administrationSubnetId", bootstrap["outputs"])

    def module_parameters(self, name):
        return self.modules[name]["properties"]["parameters"]

    def test_european_region_and_availability_defaults(self):
        parameters = self.template["parameters"]
        self.assertEqual(parameters["location"]["defaultValue"], "swedencentral")
        self.assertEqual(
            set(parameters["location"]["allowedValues"]),
            {"swedencentral", "northeurope", "westeurope"},
        )
        self.assertGreaterEqual(parameters["minimumWebInstances"]["minValue"], 2)
        self.assertEqual(parameters["frontDoorSku"]["defaultValue"], "Premium_AzureFrontDoor")
        database = self.module_parameters("postgresql")
        self.assertEqual(database["highAvailability"]["value"], "ZoneRedundant")
        self.assertEqual(database["tier"]["value"], "GeneralPurpose")
        self.assertGreaterEqual(database["backupRetentionDays"]["value"], 14)

    def test_secrets_and_passwordless_database(self):
        for name in ("entraClientSecret", "openWeatherApiKey"):
            self.assertEqual(self.template["parameters"][name]["type"].lower(), "securestring")
        database = self.module_parameters("postgresql")
        self.assertEqual(database["publicNetworkAccess"]["value"], "Disabled")
        self.assertNotIn("administratorLoginPassword", database)
        self.assertNotIn("administratorLogin", database)
        database_template = self.module_template("postgresql")
        self.assertTrue(database_template["parameters"]["administratorLoginPassword"]["nullable"])
        self.assertTrue(database_template["parameters"]["administratorLogin"]["nullable"])
        self.assertFalse(database_template["parameters"]["administratorLoginPassword"].get("defaultValue"))
        self.assertFalse(database_template["parameters"]["administratorLogin"].get("defaultValue"))
        server = next(r for r in resources(database_template)
                      if r["type"] == "Microsoft.DBforPostgreSQL/flexibleServers")
        self.assertNotIn("preview", server["apiVersion"])
        self.assertIn("administratorLoginPassword", server["properties"]["authConfig"]["passwordAuth"])
        self.assertIn("administrators", server["properties"]["authConfig"]["activeDirectoryAuth"])
        admins = json.dumps(database["administrators"])
        self.assertIn("databaseAdminGroupObjectId", admins)
        self.assertIn("databaseBootstrapObjectId", admins)
        self.assertIn("databaseBootstrapPrincipalName", admins)
        self.assertIn("ServicePrincipal", admins)
        self.assertNotIn("runtimeIdentity", admins)
        output_text = json.dumps(self.template.get("outputs", {}))
        self.assertNotIn("openWeatherApiKey", output_text)
        self.assertNotIn("entraClientSecret", output_text)

    def test_vault_is_private_and_purge_protected(self):
        vault = self.module_parameters("application-secrets")
        self.assertEqual(vault["publicNetworkAccess"]["value"], "Disabled")
        self.assertTrue(vault["enableRbacAuthorization"]["value"])
        self.assertTrue(vault["enablePurgeProtection"]["value"])
        self.assertEqual(vault["sku"]["value"], "standard")
        roles = vault["roleAssignments"]["value"]
        self.assertEqual([role["roleDefinitionIdOrName"] for role in roles], ["Key Vault Secrets User"])

    def test_web_ingress_authentication_and_tls(self):
        web_resources = resources(self.module_template("flask-web-application"))
        site = next(r for r in web_resources if r.get("name") == "web-site")
        settings = site["properties"]["parameters"]
        config = settings["siteConfig"]["value"]
        self.assertEqual(config["ipSecurityRestrictionsDefaultAction"], "Deny")
        rule = config["ipSecurityRestrictions"][0]
        self.assertEqual(rule["ipAddress"], "AzureFrontDoor.Backend")
        self.assertIn("x-azure-fdid", rule["headers"])
        self.assertEqual(config["scmIpSecurityRestrictionsDefaultAction"], "Deny")
        self.assertEqual(config["ftpsState"], "Disabled")
        self.assertEqual(config["minTlsVersion"], "1.2")
        self.assertTrue(settings["httpsOnly"]["value"])
        app_settings = {s["name"]: s["value"] for s in config["appSettings"]}
        self.assertEqual(app_settings["AUTH_MODE"], "easyauth")
        self.assertEqual(app_settings["PGSSLMODE"], "verify-full")
        self.assertIn("@Microsoft.KeyVault", app_settings["OPENWEATHER_API_KEY"])
        auth = next(r for r in web_resources if r["type"] == "Microsoft.Web/sites/config"
                    and "authsettingsV2" in r["name"])["properties"]
        self.assertTrue(auth["platform"]["enabled"])
        self.assertEqual(auth["httpSettings"]["forwardProxy"]["convention"], "Standard")
        self.assertFalse(auth["login"]["tokenStore"]["enabled"])

    def test_edge_does_not_cache_personal_responses(self):
        edge_resources = resources(self.module_template("edge-routing-and-waf"))
        route = next(r for r in edge_resources if r["type"].endswith("/routes"))
        self.assertNotIn("cacheConfiguration", route["properties"])
        self.assertEqual(route["properties"]["forwardingProtocol"], "HttpsOnly")
        self.assertEqual(route["properties"]["httpsRedirect"], "Enabled")
        waf = next(r for r in edge_resources if r.get("name") == "edge-waf-policy")
        self.assertEqual(waf["properties"]["parameters"]["policySettings"]["value"]["mode"], "Prevention")
        waf_template = waf["properties"]["template"]
        managed = waf_template["parameters"]["managedRules"]["defaultValue"]["managedRuleSets"]
        self.assertIn("Microsoft_DefaultRuleSet", {rule["ruleSetType"] for rule in managed})
        self.assertIn("Microsoft_BotManagerRuleSet", {rule["ruleSetType"] for rule in managed})
        rules = waf["properties"]["parameters"]["customRules"]["value"]["rules"]
        self.assertTrue(any(rule["ruleType"] == "RateLimitRule" and
                            rule["action"] == "Block" for rule in rules))
        self.assertTrue(any(r["type"].endswith("/securityPolicies") for r in edge_resources))

    def test_private_network_and_zone_redundant_egress(self):
        network_resources = resources(self.module_template("private-network"))
        nat = next(r for r in network_resources if r.get("name") == "zone-resilient-egress")
        self.assertEqual(nat["properties"]["parameters"]["natGatewaySku"]["value"], "StandardV2")
        vnet = next(r for r in network_resources if r.get("name") == "workload-vnet")
        subnets = vnet["properties"]["parameters"]["subnets"]["value"]
        self.assertTrue(all(s["defaultOutboundAccess"] is False for s in subnets))
        self.assertEqual(len(subnets), 4)

    def test_privacy_conscious_monitoring(self):
        for name in ("postgresql", "global-edge-profile"):
            diagnostics = self.module_parameters(name)["diagnosticSettings"]["value"]
            self.assertTrue(all("logCategoriesAndGroups" not in d for d in diagnostics))
        configuration = self.module_parameters("postgresql")["configurations"]["value"]
        values = {item["name"]: item["value"] for item in configuration}
        self.assertEqual(values["log_statement"], "none")
        self.assertEqual(values["log_min_error_statement"], "panic")

    def test_autoscale_has_a_zone_resilient_floor(self):
        operations = resources(self.module_template("scaling-and-alerts"))
        autoscale = next(r for r in operations if r["type"].lower() == "microsoft.insights/autoscalesettings")
        capacity = autoscale["properties"]["profiles"][0]["capacity"]
        self.assertIn("minimumInstances", capacity["minimum"])
        self.assertEqual(autoscale["properties"]["enabled"], True)
        self.assertTrue(autoscale["properties"]["notifications"])
