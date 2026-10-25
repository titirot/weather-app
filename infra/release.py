"""Azure-hosted private bootstrap and temporary SCM access for a hosted CI runner."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parent
RULE = "HostedRelease"


def az(*args, output="json"):
    result = subprocess.run(
        ["az", *args, "--only-show-errors", "--output", output],
        check=True, text=True, stdout=subprocess.PIPE,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def load_outputs(path):
    return {key: entry["value"] for key, entry in json.loads(Path(path).read_text()).items()}


def prepare_publishing_token():
    # Exchange the fresh OIDC assertion before lengthy app provisioning expires it.
    audience = az("cloud", "show", "--query", "endpoints.appServiceResourceId")
    if not isinstance(audience, str) or not audience.startswith("https://"):
        raise ValueError("Azure cloud has no valid App Service token audience")
    az("account", "get-access-token", "--resource", audience, output="none")


def runner_ip():
    with urlopen("https://api.ipify.org", timeout=20) as response:
        address = ipaddress.IPv4Address(response.read(64).decode().strip())
    if not address.is_global:
        raise ValueError("Runner egress must be a public IPv4")
    return str(address)


def wait_for_bootstrap(host, token):
    for attempt in range(40):
        try:
            request = Request(f"https://{host}/status", headers={"Authorization": f"Bearer {token}"})
            with urlopen(request, timeout=20) as response:
                if json.load(response) != {"status": "succeeded"}:
                    raise RuntimeError("Unexpected bootstrap status")
                return
        except HTTPError as error:
            if error.code == 500:
                failure = json.load(error)
                category = failure.get("error", "UnknownError")
                sqlstate = failure.get("sqlstate")
                if not isinstance(category, str) or not category.isidentifier():
                    raise RuntimeError("Invalid bootstrap failure category") from None
                if sqlstate is not None and (
                    not isinstance(sqlstate, str) or len(sqlstate) != 5 or not sqlstate.isalnum()
                ):
                    raise RuntimeError("Invalid bootstrap SQLSTATE") from None
                raise RuntimeError(
                    f"Private bootstrap failed ({category}; SQLSTATE {sqlstate})"
                ) from None
            if error.code not in (403, 404, 502, 503):
                raise
            print(f"Bootstrap startup attempt {attempt + 1}/40: HTTP {error.code}", flush=True)
        except (URLError, TimeoutError) as error:
            print(f"Bootstrap startup attempt {attempt + 1}/40: {type(error).__name__}", flush=True)
        time.sleep(15)
    raise RuntimeError("Private bootstrap did not report success")


def cleanup(group, outputs):
    """Retry exact-resource cleanup; errors must fail the release, not disappear."""
    apps = az("webapp", "list", "--resource-group", group)
    admins = az("postgres", "flexible-server", "microsoft-entra-admin", "list",
                "--resource-group", group, "--server-name", outputs["postgresServerName"])
    failures = []
    for exists, label, command in (
        (any(app["name"] == outputs["bootstrapWebAppName"] for app in apps),
         "temporary app", ("webapp", "delete", "--resource-group", group,
                           "--name", outputs["bootstrapWebAppName"])),
        (any(admin.get("objectId") == outputs["bootstrapIdentityObjectId"] for admin in admins),
         "temporary database administrator", (
            "postgres", "flexible-server", "microsoft-entra-admin", "delete",
            "--resource-group", group, "--server-name", outputs["postgresServerName"],
            "--object-id", outputs["bootstrapIdentityObjectId"], "--yes")),
    ):
        if not exists:
            continue
        for attempt in range(3):
            try:
                az(*command)
                break
            except subprocess.CalledProcessError:
                print(f"Cleanup {label}: attempt {attempt + 1}/3 failed", flush=True)
                if attempt == 2:
                    failures.append(label)
                else:
                    time.sleep(10)
    if failures:
        raise RuntimeError("Manual cleanup required: " + ", ".join(failures))
    admins = az("postgres", "flexible-server", "microsoft-entra-admin", "list",
                "--resource-group", group, "--server-name", outputs["postgresServerName"])
    if any(admin.get("objectId") == outputs["bootstrapIdentityObjectId"] for admin in admins):
        raise RuntimeError("Bootstrap identity remains a database administrator")
    apps = az("webapp", "list", "--resource-group", group)
    if any(app["name"] == outputs["bootstrapWebAppName"] for app in apps):
        raise RuntimeError("Temporary bootstrap app still exists")


def bootstrap(group, outputs, address):
    token = secrets.token_hex(32)
    print("::add-mask::" + token, flush=True)
    values = {
        "name": outputs["bootstrapWebAppName"], "location": outputs["deploymentLocation"],
        "planId": outputs["appServicePlanId"], "subnetId": outputs["appSubnetId"],
        "identityResourceId": outputs["bootstrapIdentityResourceId"],
        "identityClientId": outputs["bootstrapIdentityClientId"],
        "databaseHost": outputs["postgresHost"], "databaseUser": outputs["bootstrapIdentityName"],
        "runtimeRole": outputs["runtimeDatabaseRole"],
        "runtimeObjectId": outputs["runtimePrincipalObjectId"],
        "runnerIpv4": address, "statusToken": token,
    }
    try:
        az("postgres", "flexible-server", "microsoft-entra-admin", "create",
           "--resource-group", group, "--server-name", outputs["postgresServerName"],
           "--object-id", outputs["bootstrapIdentityObjectId"],
           "--display-name", outputs["bootstrapIdentityName"], "--type", "ServicePrincipal")
        with tempfile.TemporaryDirectory(prefix="skycast-bootstrap-") as directory:
            directory = Path(directory)
            parameters = directory / "parameters.json"
            parameters.write_text(json.dumps({
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                "contentVersion": "1.0.0.0",
                "parameters": {key: {"value": value} for key, value in values.items()},
            }))
            parameters.chmod(0o600)
            command = ("--resource-group", group, "--template-file", str(ROOT / "bootstrap-app.bicep"),
                       "--parameters", "@" + str(parameters))
            az("deployment", "group", "validate", *command)
            az("deployment", "group", "what-if", *command,
               "--result-format", "ResourceIdOnly", "--no-pretty-print")
            az("deployment", "group", "create", "--name", "release-private-bootstrap", *command)
            archive = directory / "bootstrap.zip"
            with ZipFile(archive, "w") as package:
                for name in ("bootstrap_app.py", "bootstrap-database.sql"):
                    package.write(ROOT / name, name)
                package.writestr(
                    "requirements.txt",
                    "Flask>=3.1,<4\ngunicorn>=23,<27\npsycopg[binary]>=3.2,<4\nazure-identity>=1.19,<2\n",
                )
            az("webapp", "deploy", "--resource-group", group,
               "--name", outputs["bootstrapWebAppName"], "--src-path", str(archive),
               "--type", "zip", "--restart", "true", "--track-status", "true")
            host = az("webapp", "show", "--resource-group", group,
                      "--name", outputs["bootstrapWebAppName"])["defaultHostName"]
            wait_for_bootstrap(host, token)
            print("Private runtime-role bootstrap verified.", flush=True)
    finally:
        cleanup(group, outputs)


def lock_scm(group, outputs):
    name = outputs["webAppName"]
    rules = az("webapp", "config", "access-restriction", "show",
               "--resource-group", group, "--name", name)
    if any(rule.get("name") == RULE for rule in rules["scmIpSecurityRestrictions"]):
        az("webapp", "config", "access-restriction", "remove", "--resource-group", group,
           "--name", name, "--rule-name", RULE, "--scm-site", "true")
    rules = az("webapp", "config", "access-restriction", "show",
               "--resource-group", group, "--name", name)
    if any(r.get("action") == "Allow" for r in rules["scmIpSecurityRestrictions"]):
        raise RuntimeError("SCM still has an allow rule; manual lockdown required")
    if rules.get("scmIpSecurityRestrictionsDefaultAction") != "Deny":
        raise RuntimeError("SCM default action is not Deny")


def publish(group, outputs, address, archive):
    name = outputs["webAppName"]
    try:
        az("webapp", "config", "access-restriction", "add", "--resource-group", group,
           "--name", name, "--rule-name", RULE, "--action", "Allow",
           "--ip-address", address + "/32", "--priority", "100", "--scm-site", "true")
        az("webapp", "deploy", "--resource-group", group, "--name", name,
           "--src-path", archive, "--type", "zip", "--restart", "true",
           "--async", "false", "--track-status", "false")
        print(
            "Application build and ZIP deployment completed. "
            "Public application smoke checks must still pass before release acceptance.",
            flush=True,
        )
    finally:
        lock_scm(group, outputs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("bootstrap", "publish", "cleanup"))
    parser.add_argument("--outputs", required=True)
    parser.add_argument("--archive")
    args = parser.parse_args()
    outputs = load_outputs(args.outputs)
    group = os.environ["AZURE_RESOURCE_GROUP"]
    if args.action == "cleanup":
        try:
            cleanup(group, outputs)
        finally:
            lock_scm(group, outputs)
        return
    address = runner_ip()
    prepare_publishing_token()
    if args.action == "bootstrap":
        bootstrap(group, outputs, address)
    else:
        if not args.archive:
            parser.error("--archive is required for publish")
        publish(group, outputs, address, args.archive)


if __name__ == "__main__":
    main()
