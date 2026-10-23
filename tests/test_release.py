import io
import os
import subprocess
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import psycopg

from infra import bootstrap_app, release


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not configured")
class BootstrapSqlIntegrationTests(unittest.TestCase):
    def test_existing_mapping_uses_case_independent_function_column_aliases(self):
        role = "bootstrap_test_" + uuid.uuid4().hex
        object_id = str(uuid.uuid4())
        source = Path(bootstrap_app.__file__).with_name("bootstrap-database.sql").read_text()
        source = source.replace("\\set ON_ERROR_STOP on\n", "").split("\\connect weather")[0]
        source = source.replace("pg_catalog.pgaadauth_", "pg_temp.pgaadauth_")
        source = source.replace(":'runtime_role'", "{role_literal}")
        source = source.replace(":'runtime_object_id'", "{object_literal}")
        statement = bootstrap_app.sql.SQL(source).format(
            role_literal=bootstrap_app.sql.Literal(role),
            object_literal=bootstrap_app.sql.Literal(object_id),
        )
        with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as connection:
            connection.execute(bootstrap_app.sql.SQL("CREATE ROLE {}").format(
                bootstrap_app.sql.Identifier(role)))
            try:
                connection.execute(
                    "CREATE TEMP TABLE bootstrap_fixture (object_id text, is_admin integer)")
                connection.execute(
                    "INSERT INTO bootstrap_fixture VALUES (%s, 0)", (object_id,))
                connection.execute(bootstrap_app.sql.SQL("""
                    CREATE FUNCTION pg_temp.pgaadauth_list_principals(boolean)
                    RETURNS TABLE(rolename name, "principalType" text, "objectId" text,
                                  "tenantId" text, "isMfa" integer, "isAdmin" integer)
                    LANGUAGE SQL AS $fixture$
                    SELECT {}::name, 'service'::text, object_id, ''::text, 0, is_admin
                    FROM bootstrap_fixture
                    $fixture$
                """).format(bootstrap_app.sql.Literal(role)))
                connection.execute("""
                    CREATE FUNCTION pg_temp.pgaadauth_create_principal_with_oid(
                        text, text, text, boolean, boolean)
                    RETURNS text LANGUAGE plpgsql AS $fixture$
                    BEGIN RAISE EXCEPTION 'Existing mapped role must not be recreated'; END
                    $fixture$
                """)
                for column in ("objectid", "isadmin"):
                    with self.subTest(unquoted_column=column):
                        with self.assertRaises(psycopg.errors.UndefinedColumn) as error:
                            connection.execute(bootstrap_app.sql.SQL(
                                "SELECT {} FROM pg_temp.pgaadauth_list_principals(false)"
                            ).format(bootstrap_app.sql.Identifier(column)))
                        self.assertEqual(error.exception.sqlstate, "42703")
                for attempt in range(2):
                    with self.subTest(idempotent_attempt=attempt):
                        connection.execute(statement, prepare=False)
                for mapped_id, is_admin in ((str(uuid.uuid4()), 0), (object_id, 1)):
                    with self.subTest(mapped_id=mapped_id, is_admin=is_admin):
                        connection.execute(
                            "UPDATE bootstrap_fixture SET object_id = %s, is_admin = %s",
                            (mapped_id, is_admin))
                        try:
                            with self.assertRaisesRegex(
                                psycopg.errors.RaiseException, "unexpected Entra mapping"
                            ):
                                connection.execute(statement, prepare=False)
                        finally:
                            connection.execute("ROLLBACK")
                connection.execute(
                    "UPDATE bootstrap_fixture SET object_id = %s, is_admin = 0", (object_id,))
                connection.execute(bootstrap_app.sql.SQL("ALTER ROLE {} CREATEROLE").format(
                    bootstrap_app.sql.Identifier(role)))
                with self.assertRaisesRegex(
                    psycopg.errors.RaiseException, "must not have server administration privileges"
                ):
                    connection.execute(statement, prepare=False)
            finally:
                connection.execute("ROLLBACK")
                connection.execute(bootstrap_app.sql.SQL("DROP ROLE {}").format(
                    bootstrap_app.sql.Identifier(role)))


class BootstrapTests(unittest.TestCase):
    def test_status_requires_token_and_does_not_expose_failure(self):
        token = "a" * 64
        with patch.dict(os.environ, {"BOOTSTRAP_STATUS_TOKEN": token}):
            app = bootstrap_app.create_app(lambda: None)
            client = app.test_client()
            self.assertEqual(client.get("/status").status_code, 401)
            result = client.get("/status", headers={"Authorization": "Bearer " + token})
            self.assertEqual(result.json, {"status": "succeeded"})
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            with self.assertLogs(app.logger, level="ERROR") as logs:
                failed = bootstrap_app.create_app(
                    lambda: (_ for _ in ()).throw(psycopg.OperationalError("secret-value"))
                )
            self.assertNotIn("secret-value", "\n".join(logs.output))
            result = failed.test_client().get(
                "/status", headers={"Authorization": "Bearer " + token}
            )
            self.assertEqual(result.status_code, 500)
            self.assertEqual(result.json, {"status": "failed", "error": "OperationalError",
                                          "sqlstate": None})
            self.assertNotIn("secret-value", result.get_data(as_text=True))

    def test_private_sql_uses_verified_tls_and_safe_role_quoting(self):
        env = {
            "RUNTIME_ROLE": 'runtime"role',
            "RUNTIME_OBJECT_ID": "11111111-1111-1111-1111-111111111111",
            "AZURE_CLIENT_ID": "identity-client",
            "PGHOST": "private.database.azure.com",
            "PGUSER": "bootstrap-admin",
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (True, True, True)
        with patch.dict(os.environ, env), patch.object(
            bootstrap_app, "ManagedIdentityCredential"
        ) as credential, patch.object(bootstrap_app.psycopg, "connect", return_value=connection) as connect:
            credential.return_value.get_token.return_value.token = "transient-token"
            bootstrap_app.run_bootstrap()
            self.assertEqual(connect.call_count, 2)
            for call in connect.call_args_list:
                self.assertEqual(call.kwargs["sslmode"], "verify-full")
                self.assertEqual(call.kwargs["user"], "bootstrap-admin")
            rendered = [call.args[0].as_string() for call in connection.execute.call_args_list
                        if isinstance(call.args[0], bootstrap_app.sql.Composable)]
            self.assertEqual(len(rendered), 2)
            self.assertNotIn("\\connect", "".join(rendered))
            self.assertIn('"runtime""role"', rendered[1])
            self.assertIn("pgaadauth_create_principal_with_oid", rendered[0])
            credential.return_value.close.assert_called_once()

    def test_missing_grants_fail_explicitly(self):
        env = {
            "RUNTIME_ROLE": "runtime",
            "RUNTIME_OBJECT_ID": "11111111-1111-1111-1111-111111111111",
            "AZURE_CLIENT_ID": "identity",
            "PGHOST": "private",
            "PGUSER": "bootstrap",
        }
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.execute.return_value.fetchone.return_value = (True, False, True)
        with patch.dict(os.environ, env), patch.object(
            bootstrap_app, "ManagedIdentityCredential"
        ), patch.object(bootstrap_app.psycopg, "connect", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "grants"):
                bootstrap_app.run_bootstrap()


class ReleaseTests(unittest.TestCase):
    def test_bootstrap_failure_reports_safe_sqlstate_before_cleanup(self):
        error = HTTPError("https://private/status", 500, "failure", {},
                          io.BytesIO(b'{"status":"failed","error":"InsufficientPrivilege","sqlstate":"42501"}'))
        with patch.object(release, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "InsufficientPrivilege; SQLSTATE 42501"):
                release.wait_for_bootstrap("private", "token")

    def test_publishing_token_is_acquired_for_the_current_cloud_without_output(self):
        with patch.object(release, "az", side_effect=["https://appservice.azure.com", None]) as azure:
            release.prepare_publishing_token()
            azure.assert_any_call("cloud", "show", "--query", "endpoints.appServiceResourceId")
            azure.assert_called_with(
                "account", "get-access-token", "--resource", "https://appservice.azure.com",
                output="none",
            )

    def test_missing_publishing_audience_fails_before_requesting_token(self):
        with patch.object(release, "az", return_value=None) as azure:
            with self.assertRaisesRegex(ValueError, "audience"):
                release.prepare_publishing_token()
            self.assertEqual(azure.call_count, 1)

    def test_publish_failure_still_locks_scm(self):
        failure = subprocess.CalledProcessError(1, "az")
        with patch.object(release, "az", side_effect=[None, failure]), patch.object(
            release, "lock_scm"
        ) as lock:
            with self.assertRaises(subprocess.CalledProcessError):
                release.publish("group", {"webAppName": "web"}, "203.0.113.1", "app.zip")
            lock.assert_called_once_with("group", {"webAppName": "web"})

    def test_lock_scm_rejects_unexpected_allow_rules(self):
        rules = {"scmIpSecurityRestrictions": [{"name": "unexpected", "action": "Allow"}],
                 "scmIpSecurityRestrictionsDefaultAction": "Deny"}
        with patch.object(release, "az", return_value=rules):
            with self.assertRaisesRegex(RuntimeError, "allow rule"):
                release.lock_scm("group", {"webAppName": "web"})

    def test_cleanup_is_idempotent_when_resources_absent(self):
        outputs = {"bootstrapWebAppName": "temp", "postgresServerName": "pg",
                   "bootstrapIdentityObjectId": "bootstrap-object"}
        with patch.object(release, "az", return_value=[]) as azure:
            release.cleanup("group", outputs)
            self.assertFalse(any("delete" in call.args for call in azure.call_args_list))

    def test_cleanup_deletes_only_temporary_resources(self):
        outputs = {"bootstrapWebAppName": "temp", "postgresServerName": "pg",
                   "bootstrapIdentityObjectId": "bootstrap-object"}
        responses = [
            [{"name": "temp"}, {"name": "customer-web"}],
            [{"objectId": "bootstrap-object"}, {"objectId": "human-admin"}],
            None, None, [{"objectId": "human-admin"}], [{"name": "customer-web"}],
        ]
        with patch.object(release, "az", side_effect=responses) as azure:
            release.cleanup("group", outputs)
            deletes = [call.args for call in azure.call_args_list if "delete" in call.args]
            self.assertEqual(len(deletes), 2)
            self.assertIn("temp", deletes[0])
            self.assertIn("bootstrap-object", deletes[1])
            self.assertNotIn("human-admin", deletes[1])


if __name__ == "__main__":
    unittest.main()
