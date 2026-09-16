import csv
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import bulk_migrate
import inventory
import migrate_function_app

ENVIRONMENT_ID = (
    "/subscriptions/sub/resourceGroups/rg/providers/"
    "Microsoft.App/managedEnvironments/source-env"
)


def sample_v1_metadata():
    return {
        "name": "source-app",
        "location": "eastus",
        "kind": "functionapp,linux,container,azurecontainerapps",
        "identity": None,
        "tags": {"team": "functions"},
        "properties": {
            "function_app": {
                "id": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/source-app",
                "name": "source-app",
                "type": "Microsoft.Web/sites",
                "kind": "functionapp,linux,container,azurecontainerapps",
                "location": "eastus",
                "properties": {},
            },
            "managed_environment_id": ENVIRONMENT_ID,
            "backing_container_app_id": (
                "/subscriptions/sub/resourceGroups/managed/providers/"
                "Microsoft.App/containerApps/source-app"
            ),
            "app_settings": [
                {"name": "FUNCTIONS_EXTENSION_VERSION", "value": "~4"},
                {"name": "AzureWebJobsStorage", "value": "storage-secret"},
            ],
            "connection_strings": [],
            "backing_container_app": {
                "location": "eastus",
                "kind": None,
                "managedBy": "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Web/sites/source-app",
                "tags": {"managed": "true"},
                "identity": {"type": "None"},
                "properties": {
                    "environmentId": ENVIRONMENT_ID,
                    "workloadProfileName": "Consumption",
                    "configuration": {
                        "activeRevisionsMode": "Single",
                        "dapr": {
                            "enabled": True,
                            "appId": "source-app",
                            "appPort": 8080,
                        },
                        "ingress": {
                            "external": True,
                            "targetPort": 80,
                            "transport": "Auto",
                            "fqdn": "source.example.test",
                            "traffic": [
                                {
                                    "latestRevision": True,
                                    "revisionName": "source--abc",
                                    "weight": 100,
                                }
                            ],
                        },
                        "secrets": [
                            {"name": "functions-version"},
                            {"name": "storage"},
                        ],
                    },
                    "template": {
                        "revisionSuffix": "old",
                        "containers": [
                            {
                                "name": "functions-container",
                                "image": "mcr.microsoft.com/functions:test",
                                "env": [
                                    {
                                        "name": "FUNCTIONS_EXTENSION_VERSION",
                                        "secretRef": "functions-version",
                                    },
                                    {
                                        "name": "AzureWebJobsStorage",
                                        "secretRef": "storage",
                                    },
                                    {
                                        "name": "WEBSITE_SITE_NAME",
                                        "secretRef": "proxy-default",
                                    },
                                ],
                                "resources": {
                                    "cpu": 1.0,
                                    "memory": "2Gi",
                                    "ephemeralStorage": "8Gi",
                                },
                                "probes": [{"type": "Liveness"}],
                            }
                        ],
                        "scale": {
                            "minReplicas": 0,
                            "maxReplicas": 30,
                            "cooldownPeriod": 300,
                            "pollingInterval": 30,
                        },
                        "volumes": [{"name": "data", "storageType": "EmptyDir"}],
                    },
                },
            },
        },
    }


class InventoryTests(unittest.TestCase):
    def test_only_legacy_aca_proxy_kind_is_v1(self):
        self.assertTrue(
            migrate_function_app.is_v1_function_app(
                "Microsoft.Web/sites",
                "functionapp,linux,container,azurecontainerapps",
            )
        )
        self.assertFalse(
            migrate_function_app.is_v1_function_app(
                "Microsoft.Web/sites",
                "functionapp",
            )
        )

    def test_default_target_name_is_valid_and_collision_resistant(self):
        target = migrate_function_app.default_target_app_name(
            "shkr-perfapp-net9-easia-contapps"
        )
        other = migrate_function_app.default_target_app_name(
            "shkr-perfapp-net9-easia-contapps-other"
        )
        self.assertIsNone(
            migrate_function_app.validate_container_app_name(target)
        )
        self.assertLessEqual(len(target), 32)
        self.assertNotEqual(target, other)

    def test_json_mode_writes_only_json(self):
        apps = [
            {
                "subscription_id": "sub",
                "resource_group": "rg",
                "name": "app",
                "location": "eastus",
            }
        ]
        stdout = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["inventory.py", "--subscription-id", "sub", "--json"],
            ),
            patch.object(inventory, "list_v1_function_apps", return_value=apps),
            redirect_stdout(stdout),
        ):
            inventory.main()
        output = json.loads(stdout.getvalue())
        self.assertEqual(1, output["summary"]["total_v1_apps"])

    def test_blocked_apps_are_shown_with_reason(self):
        stdout = io.StringIO()
        app = {
            "subscription_id": "sub",
            "resource_group": "rg",
            "name": "broken",
            "location": "eastus",
            "migration_status": "Blocked",
            "migration_blocker": "Backing Container App was not found",
        }
        with redirect_stdout(stdout):
            inventory.print_inventory([app])
        self.assertIn("Migration blockers", stdout.getvalue())
        self.assertIn("Backing Container App was not found", stdout.getvalue())

    def test_migrated_apps_show_destination(self):
        stdout = io.StringIO()
        app = {
            "subscription_id": "sub",
            "resource_group": "rg",
            "name": "source-app",
            "location": "eastus",
            "migration_status": "Migrated",
            "destination_app_name": "target-app",
        }
        with redirect_stdout(stdout):
            inventory.print_inventory([app])
        self.assertIn("Already migrated", stdout.getvalue())
        self.assertIn("target-app", stdout.getvalue())


class MigrationTests(unittest.TestCase):
    def test_live_backing_body_is_copied_and_sanitized(self):
        transformed = migrate_function_app.transform_to_v2(
            sample_v1_metadata(),
            "target-app",
        )
        body = migrate_function_app.build_container_app_body(
            transformed,
            ENVIRONMENT_ID,
        )
        container = body["properties"]["template"]["containers"][0]
        ingress = body["properties"]["configuration"]["ingress"]

        self.assertEqual("functionapp", body["kind"])
        self.assertEqual("true", body["tags"]["migrate-via-tool"])
        self.assertEqual(
            sample_v1_metadata()["properties"]["function_app"]["id"],
            body["tags"]["migration-source-id"],
        )
        self.assertNotIn("managedBy", body)
        self.assertEqual(ENVIRONMENT_ID, body["properties"]["environmentId"])
        self.assertEqual("8Gi", container["resources"]["ephemeralStorage"])
        self.assertEqual(30, body["properties"]["template"]["scale"]["maxReplicas"])
        self.assertEqual("Auto", ingress["transport"])
        self.assertNotIn("fqdn", ingress)
        self.assertNotIn("revisionName", ingress["traffic"][0])
        self.assertNotIn("revisionSuffix", body["properties"]["template"])
        self.assertEqual(
            "target-app",
            body["properties"]["configuration"]["dapr"]["appId"],
        )
        self.assertEqual([{"type": "Liveness"}], container["probes"])
        self.assertEqual(
            [{"name": "data", "storageType": "EmptyDir"}],
            body["properties"]["template"]["volumes"],
        )

    def test_secret_values_are_rehydrated_and_proxy_defaults_removed(self):
        transformed = migrate_function_app.transform_to_v2(
            sample_v1_metadata(),
            "target-app",
        )
        body = migrate_function_app.build_container_app_body(
            transformed,
            ENVIRONMENT_ID,
        )
        configuration = body["properties"]["configuration"]
        env = body["properties"]["template"]["containers"][0]["env"]
        secret_values = {
            secret["name"]: secret["value"]
            for secret in configuration["secrets"]
        }
        self.assertEqual("~4", secret_values["functions-version"])
        self.assertEqual("storage-secret", secret_values["storage"])
        self.assertNotIn("WEBSITE_SITE_NAME", {item["name"] for item in env})

    def test_system_registry_identity_requires_create(self):
        source = sample_v1_metadata()
        backing = source["properties"]["backing_container_app"]
        backing["identity"] = {"type": "SystemAssigned"}
        backing["properties"]["configuration"]["registries"] = [
            {"server": "example.azurecr.io", "identity": "system"}
        ]
        with self.assertRaisesRegex(ValueError, "system identity"):
            migrate_function_app.transform_to_v2(source, "target-app")
        transformed = migrate_function_app.transform_to_v2(
            source,
            "target-app",
            system_identity_action="create",
        )
        body = migrate_function_app.build_container_app_body(
            transformed,
            ENVIRONMENT_ID,
        )
        self.assertEqual("SystemAssigned", body["identity"]["type"])

    def test_connection_strings_are_added_as_secret_backed_env(self):
        source = sample_v1_metadata()
        source["properties"]["connection_strings"] = [
            {"name": "MainDb", "value": "db-secret", "type": "SQLAzure"}
        ]
        transformed = migrate_function_app.transform_to_v2(
            source,
            "target-app",
        )
        settings = {
            item["name"]: item["value"]
            for item in transformed["properties"]["site_config"]["app_settings"]
        }
        self.assertEqual("db-secret", settings["SQLAZURECONNSTR_MainDb"])

    def test_tagged_destination_is_migration_checkpoint(self):
        source_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Web/sites/source-app"
        )
        target = type(
            "Target",
            (),
            {
                "tags": {
                    "migrate-via-tool": "true",
                    "migration-source-id": source_id,
                }
            },
        )()
        with patch.object(
            migrate_function_app,
            "get_target_container_app",
            return_value=target,
        ):
            state = migrate_function_app.ensure_target_available(
                "sub",
                "rg",
                "target-app",
                source_id,
            )
        self.assertEqual("migrated", state)

    def test_untagged_destination_is_not_overwritten(self):
        target = type("Target", (), {"tags": {}})()
        with patch.object(
            migrate_function_app,
            "get_target_container_app",
            return_value=target,
        ):
            with self.assertRaisesRegex(ValueError, "already exists"):
                migrate_function_app.ensure_target_available(
                    "sub",
                    "rg",
                    "target-app",
                    "/subscriptions/sub/resourceGroups/rg/providers/"
                    "Microsoft.Web/sites/source-app",
                )

    def test_legacy_tool_tag_requires_rerun_choice(self):
        target = type(
            "Target",
            (),
            {"tags": {"migrate-via-tool": "true"}},
        )()
        with patch.object(
            migrate_function_app,
            "get_target_container_app",
            return_value=target,
        ):
            state = migrate_function_app.ensure_target_available(
                "sub",
                "rg",
                "target-app",
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/source-app",
            )
        self.assertEqual("tool-created", state)

    def test_migrated_app_health_reports_resource_and_checkpoint(self):
        source_id = (
            "/subscriptions/sub/resourceGroups/rg/providers/"
            "Microsoft.Web/sites/source-app"
        )
        target = type(
            "Target",
            (),
            {
                "tags": {
                    "migrate-via-tool": "true",
                    "migration-source-id": source_id,
                },
                "properties": {
                    "provisioningState": "Succeeded",
                    "runningStatus": "Running",
                    "latestReadyRevisionName": "target--abc",
                    "configuration": {"ingress": {"fqdn": "target.example.test"}},
                    "template": {"containers": [{"name": "functions-container"}]},
                },
            },
        )()
        with patch.object(
            migrate_function_app,
            "get_target_container_app",
            return_value=target,
        ):
            report = migrate_function_app.check_migrated_app_health(
                "sub",
                "rg",
                "target-app",
                source_id,
            )
        self.assertTrue(report["healthy"])
        self.assertTrue(report["checkpoint_match"])

    def test_post_migration_health_waits_until_ready(self):
        with (
            patch.object(
                migrate_function_app,
                "check_migrated_app_health",
                side_effect=[
                    {"healthy": False, "checkpoint_match": True},
                    {"healthy": True, "checkpoint_match": True},
                ],
            ) as health_check,
            patch.object(migrate_function_app.time, "sleep") as sleep,
        ):
            report = migrate_function_app.wait_for_migrated_app_health(
                "sub",
                "rg",
                "target-app",
                "/subscriptions/sub/resourceGroups/rg/providers/"
                "Microsoft.Web/sites/source-app",
                attempts=2,
                interval_seconds=0,
            )
        self.assertTrue(report["healthy"])
        self.assertEqual(2, health_check.call_count)
        sleep.assert_called_once_with(0)

    def test_checkpoint_csv_is_append_only_and_health_checkable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/migration-checkpoints.csv"
            migrate_function_app.append_migration_checkpoint(
                path,
                "sub",
                "source-rg",
                "source-app",
                "target-rg",
                "target-app",
                ENVIRONMENT_ID,
            )
            migrate_function_app.append_migration_checkpoint(
                path,
                "sub",
                "source-rg",
                "source-app-2",
                "target-rg",
                "target-app-2",
                ENVIRONMENT_ID,
            )
            with patch.object(
                migrate_function_app,
                "check_migrated_app_health",
                return_value={
                    "healthy": True,
                    "checkpoint_match": True,
                },
            ):
                healthy = migrate_function_app.check_checkpoint_csv(path)
            with open(path, newline="", encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
        self.assertTrue(healthy)
        self.assertEqual(2, len(rows))

class BulkMigrationTests(unittest.TestCase):
    def test_bulk_output_names_use_input_stem_in_current_directory(self):
        input_csv = "/other/directory/example-plan.csv"
        self.assertEqual(
            Path.cwd() / "example-plan-checkpoints.csv",
            bulk_migrate.checkpoint_csv_for_input(input_csv),
        )
        self.assertEqual(
            Path.cwd() / "example-plan-results.json",
            bulk_migrate.results_json_for_input(input_csv),
        )

    def test_blocked_inventory_row_is_not_a_csv_schema_error(self):
        errors = bulk_migrate.validate_csv(
            [
                {
                    "source_app_name": "broken",
                    "source_resource_group": "rg",
                    "source_subscription_id": "sub",
                    "migration_status": "Blocked",
                    "migration_blocker": "Backing Container App was not found",
                }
            ]
        )
        self.assertEqual([], errors)

    def test_pending_rows_require_destination(self):
        errors = bulk_migrate.validate_csv(
            [
                {
                    "source_app_name": "source",
                    "source_resource_group": "rg",
                    "source_subscription_id": "sub",
                    "migration_status": "Ready",
                    "target_resource_group": "",
                    "target_app_name": "",
                }
            ]
        )
        self.assertEqual(2, len(errors))


if __name__ == "__main__":
    unittest.main()
