#!/usr/bin/env python3
"""Migrate a legacy Functions proxy app by copying its live backing Container App."""

import argparse
import copy
import csv
import datetime
import hashlib
import re
import sys
import time
from pathlib import Path

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.mgmt.resource.resources import ResourceManagementClient
from azure.mgmt.web import WebSiteManagementClient

WEB_SITES_API_VERSION = "2026-03-15"
CONTAINER_APPS_API_VERSION = "2026-01-01"
PROXY_DEFAULT_ENV_NAMES = {
    "DAPR_APP_PORT",
    "MANAGED_ENVIRONMENT",
    "WEBSITE_SITE_NAME",
}
CHECKPOINT_FIELDS = [
    "migrated_at",
    "source_app_id",
    "target_app_id",
    "subscription_id",
    "source_resource_group",
    "source_app_name",
    "target_resource_group",
    "target_app_name",
    "environment_id",
]


def is_v1_function_app(resource_type, kind):
    kind_parts = {part.strip().lower() for part in (kind or "").split(",")}
    return (
        (resource_type or "").lower() == "microsoft.web/sites"
        and "functionapp" in kind_parts
        and "azurecontainerapps" in kind_parts
    )


def default_target_app_name(source_name):
    normalized = re.sub(r"[^a-z0-9-]", "-", source_name.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"f-{normalized}"
    if len(normalized) <= 29:
        return f"{normalized}-v2"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:6]
    return f"{normalized[:22].rstrip('-')}-{digest}-v2"


def validate_container_app_name(name):
    if not 2 <= len(name) <= 32:
        return "must be between 2 and 32 characters"
    if "--" in name:
        return "must not contain consecutive hyphens"
    if not re.fullmatch(r"[a-z][a-z0-9-]*[a-z0-9]", name):
        return (
            "must contain lowercase letters, numbers, and hyphens, start with "
            "a letter, and end with a letter or number"
        )
    return None


def _parse_source_app_id(resource_id):
    if not resource_id:
        return {}
    pattern = (
        r"/subscriptions/([^/]+)/resourceGroups/([^/]+)"
        r"(?:/providers/([^/]+)/([^/]+)/([^/?#]+))?"
    )
    match = re.fullmatch(pattern, resource_id, re.IGNORECASE)
    if not match:
        return {}
    return {
        "subscription_id": match.group(1),
        "resource_group": match.group(2),
        "provider": match.group(3),
        "resource_type": match.group(4),
        "resource_name": match.group(5),
    }


def _as_dict(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return None


def _sanitize_secret_name(name, used_names):
    normalized = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") or "secret"
    candidate = normalized[:253]
    if candidate in used_names:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        candidate = f"{normalized[:244]}-{digest}"
    used_names.add(candidate)
    return candidate


def _connection_string_setting_name(connection):
    connection_type = re.sub(
        r"[^a-z]",
        "",
        str(connection.get("type") or "Custom").lower(),
    )
    if "sqlazure" in connection_type:
        prefix = "SQLAZURECONNSTR_"
    elif "sqlserver" in connection_type:
        prefix = "SQLCONNSTR_"
    elif "mysql" in connection_type:
        prefix = "MYSQLCONNSTR_"
    elif "postgres" in connection_type:
        prefix = "POSTGRESQLCONNSTR_"
    else:
        prefix = "CUSTOMCONNSTR_"
    return f"{prefix}{connection['name']}"


def transform_app_settings(app_settings, connection_strings=None):
    settings = {
        item["name"]: item.get("value", "")
        for item in app_settings or []
        if item.get("name")
    }
    for connection in connection_strings or []:
        if connection.get("name"):
            settings[_connection_string_setting_name(connection)] = connection.get(
                "value",
                "",
            )
    return settings


def _response_properties(response):
    if isinstance(response, dict):
        return response.get("properties")
    return getattr(response, "properties", None)


def export_v1_metadata(subscription_id, resource_group, app_name):
    """Read the proxy app, its settings, and its live backing Container App."""
    print(f"\n[Step 1] Reading v1 proxy and backing app for {app_name}...")
    credential = DefaultAzureCredential()
    resource_client = ResourceManagementClient(credential, subscription_id)
    web_client = WebSiteManagementClient(credential, subscription_id)
    site_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Web/sites/{app_name}"
    )

    try:
        site = resource_client.resources.get_by_id(
            site_id,
            api_version=WEB_SITES_API_VERSION,
        )
    except ResourceNotFoundError as error:
        raise RuntimeError(f"Function App not found: {site_id}") from error

    if not is_v1_function_app(site.type, site.kind):
        raise ValueError(
            f"{app_name} is not a legacy Functions-on-Container-Apps v1 app"
        )

    from inventory import list_v1_function_apps

    matches = [
        item
        for item in list_v1_function_apps(
            [subscription_id],
            resource_group=resource_group,
            credential=credential,
        )
        if item["name"].lower() == app_name.lower()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one backing Container App for {app_name}, found {len(matches)}"
        )
    inventory_entry = matches[0]
    if inventory_entry["migration_status"] == "Blocked":
        raise ValueError(inventory_entry["migration_blocker"])

    backing = resource_client.resources.get_by_id(
        inventory_entry["backing_container_app_id"],
        api_version=CONTAINER_APPS_API_VERSION,
    )
    app_settings_result = web_client.web_apps.list_application_settings(
        resource_group,
        app_name,
    )
    app_settings = [
        {"name": name, "value": value}
        for name, value in (_response_properties(app_settings_result) or {}).items()
    ]

    connection_result = web_client.web_apps.list_connection_strings(
        resource_group,
        app_name,
    )
    connection_strings = []
    connection_properties = _response_properties(connection_result) or []
    if isinstance(connection_properties, dict):
        connection_items = connection_properties.items()
    else:
        connection_items = (
            (
                item.get("name"),
                item,
            )
            for item in connection_properties
            if isinstance(item, dict) and item.get("name")
        )
    for name, info in connection_items:
        if isinstance(info, dict):
            value = (
                info.get("connectionString")
                or info.get("connection_string")
                or info.get("value")
            )
            connection_type = info.get("type", "Custom")
        else:
            value = getattr(info, "connection_string", None)
            connection_type = getattr(info, "type", "Custom")
        connection_strings.append(
            {
                "name": name,
                "value": value,
                "type": str(connection_type),
            }
        )

    metadata = {
        "name": site.name,
        "location": site.location,
        "kind": site.kind,
        "identity": _as_dict(getattr(site, "identity", None)),
        "tags": site.tags or {},
        "properties": {
            "function_app": {
                "id": site.id,
                "name": site.name,
                "type": site.type,
                "apiVersion": WEB_SITES_API_VERSION,
                "kind": site.kind,
                "location": site.location,
                "tags": site.tags or {},
                "identity": _as_dict(getattr(site, "identity", None)),
                "properties": copy.deepcopy(site.properties or {}),
            },
            "managed_environment_id": inventory_entry[
                "container_app_environment_id"
            ],
            "backing_container_app_id": inventory_entry[
                "backing_container_app_id"
            ],
            "backing_container_app": {
                "id": backing.id,
                "name": backing.name,
                "type": backing.type,
                "apiVersion": CONTAINER_APPS_API_VERSION,
                "location": backing.location,
                "kind": backing.kind,
                "managedBy": getattr(backing, "managed_by", None),
                "tags": backing.tags or {},
                "identity": _as_dict(getattr(backing, "identity", None)),
                "properties": copy.deepcopy(backing.properties or {}),
            },
            "app_settings": app_settings,
            "connection_strings": connection_strings,
        },
    }
    print(f"Backing app: {inventory_entry['backing_container_app_id']}")
    return metadata


def _identity_parts(identity):
    identity = identity or {}
    identity_type = str(identity.get("type") or "").lower().replace("_", "")
    user_identities = (
        identity.get("userAssignedIdentities")
        or identity.get("user_assigned_identities")
        or {}
    )
    return "systemassigned" in identity_type, set(user_identities)


def _apply_identity(body, source_identity, system_identity_action):
    backing_has_system, backing_users = _identity_parts(body.get("identity"))
    source_has_system, source_users = _identity_parts(source_identity)
    user_ids = backing_users | source_users
    has_system = backing_has_system or source_has_system

    registries = body.get("properties", {}).get("configuration", {}).get(
        "registries"
    ) or []
    registry_uses_system = any(
        str(registry.get("identity") or "").lower() == "system"
        for registry in registries
    )
    has_system = has_system or registry_uses_system

    if has_system and system_identity_action not in {"create", "skip"}:
        raise ValueError(
            "The backing app uses a system identity. Choose "
            "--system-identity-action create or skip."
        )
    if registry_uses_system and system_identity_action == "skip":
        raise ValueError(
            "Registry authentication uses the system identity, so it cannot be skipped."
        )

    include_system = has_system and system_identity_action == "create"
    if not include_system and not user_ids:
        body.pop("identity", None)
        return

    if include_system and user_ids:
        identity_type = "SystemAssigned,UserAssigned"
    elif include_system:
        identity_type = "SystemAssigned"
    else:
        identity_type = "UserAssigned"
    body["identity"] = {"type": identity_type}
    if user_ids:
        body["identity"]["userAssignedIdentities"] = {
            identity_id: {} for identity_id in sorted(user_ids)
        }


def _rehydrate_settings(body, settings, target_app_name):
    properties = body["properties"]
    configuration = properties.setdefault("configuration", {})
    template = properties.setdefault("template", {})
    containers = template.get("containers") or []
    if not containers:
        raise ValueError("Backing Container App has no containers")

    primary = containers[0]
    existing_env = primary.get("env") or []
    secret_definitions = {
        secret.get("name"): secret
        for secret in configuration.get("secrets") or []
        if secret.get("name")
    }
    used_secret_names = set(secret_definitions)
    required_secrets = {}
    transformed_env = []
    applied_settings = set()

    for environment_variable in existing_env:
        name = environment_variable.get("name")
        if not name:
            continue
        if name in PROXY_DEFAULT_ENV_NAMES:
            continue
        if name in settings:
            secret_ref = environment_variable.get("secretRef") or _sanitize_secret_name(
                name,
                used_secret_names,
            )
            transformed_env.append({"name": name, "secretRef": secret_ref})
            required_secrets[secret_ref] = {
                "name": secret_ref,
                "value": "" if settings[name] is None else str(settings[name]),
            }
            applied_settings.add(name)
            continue
        if environment_variable.get("value") is not None:
            transformed_env.append(copy.deepcopy(environment_variable))
            continue
        secret_ref = environment_variable.get("secretRef")
        definition = secret_definitions.get(secret_ref)
        if definition and definition.get("keyVaultUrl"):
            transformed_env.append(copy.deepcopy(environment_variable))
            required_secrets[secret_ref] = copy.deepcopy(definition)
            continue
        raise ValueError(
            f"Secret value for environment variable {name} is unavailable"
        )

    for name, value in settings.items():
        if name in applied_settings:
            continue
        secret_ref = _sanitize_secret_name(name, used_secret_names)
        transformed_env.append({"name": name, "secretRef": secret_ref})
        required_secrets[secret_ref] = {
            "name": secret_ref,
            "value": "" if value is None else str(value),
        }

    primary["env"] = transformed_env
    for registry in configuration.get("registries") or []:
        password_ref = registry.get("passwordSecretRef")
        if not password_ref:
            continue
        password = settings.get("DOCKER_REGISTRY_SERVER_PASSWORD")
        if password is None:
            raise ValueError(
                f"Registry password value is unavailable for secret {password_ref}"
            )
        required_secrets[password_ref] = {
            "name": password_ref,
            "value": str(password),
        }

    configuration["secrets"] = list(required_secrets.values())
    dapr = configuration.get("dapr")
    if dapr:
        dapr["appId"] = target_app_name


def transform_to_v2(
    v1_metadata,
    target_app_name,
    system_identity_action=None,
):
    """Copy and sanitize the proven live backing Container App ARM document."""
    print("\n[Step 2] Copying live backing Container App configuration...")
    backing = (
        v1_metadata.get("properties", {}).get("backing_container_app")
        or {}
    )
    if not backing:
        raise ValueError("Live backing Container App ARM metadata is required")

    backing_properties = copy.deepcopy(backing.get("properties") or {})
    body = {
        "location": backing.get("location"),
        "kind": "functionapp",
        "tags": {
            **copy.deepcopy(v1_metadata.get("tags") or backing.get("tags") or {}),
            "migrate-via-tool": "true",
            "migration-source-id": v1_metadata["properties"]["function_app"]["id"],
        },
        "properties": {
            key: backing_properties[key]
            for key in (
                "configuration",
                "environmentId",
                "template",
                "workloadProfileName",
            )
            if key in backing_properties
        },
    }
    if not body["properties"].get("environmentId"):
        raise ValueError("Backing Container App does not contain environmentId")

    backing_identity = copy.deepcopy(backing.get("identity"))
    if backing_identity:
        body["identity"] = backing_identity

    configuration = body["properties"].setdefault("configuration", {})
    template = body["properties"].setdefault("template", {})
    template.pop("revisionSuffix", None)
    ingress = configuration.get("ingress") or {}
    ingress.pop("fqdn", None)
    for traffic in ingress.get("traffic") or []:
        traffic.pop("revisionName", None)

    containers = template.get("containers") or []
    if not containers:
        raise ValueError("Backing Container App has no containers")
    primary = containers[0]
    settings = transform_app_settings(
        v1_metadata.get("properties", {}).get("app_settings"),
        v1_metadata.get("properties", {}).get("connection_strings"),
    )
    _rehydrate_settings(body, settings, target_app_name)
    _apply_identity(body, v1_metadata.get("identity"), system_identity_action)

    deployment_properties = {
        "container_image": primary.get("image"),
        "workload_profile_name": body["properties"].get("workloadProfileName"),
        "resources": copy.deepcopy(primary.get("resources") or {}),
        "dapr": copy.deepcopy(configuration.get("dapr")),
        "ingress": copy.deepcopy(configuration.get("ingress")),
        "scale": copy.deepcopy(template.get("scale") or {}),
        "request_body": body,
        "warnings": [],
    }
    print("Live backing configuration copied and sanitized")
    return {
        "name": target_app_name,
        "location": body["location"],
        "kind": "functionapp",
        "tags": body.get("tags") or {},
        "properties": {
            "site_config": {
                "app_settings": [
                    {"name": name, "value": value}
                    for name, value in settings.items()
                ]
            }
        },
        "deployment_properties": deployment_properties,
    }


def build_container_app_body(v2_metadata, environment_id):
    body = copy.deepcopy(
        v2_metadata.get("deployment_properties", {}).get("request_body")
        or {}
    )
    if not body:
        raise ValueError("Transformed backing Container App request body is missing")
    if body.get("kind") != "functionapp":
        raise ValueError("Target request kind must be functionapp")
    if body.get("properties", {}).get("environmentId") != environment_id:
        raise ValueError("Target request must preserve the source environmentId")
    if not body.get("properties", {}).get("template", {}).get("containers"):
        raise ValueError("Target request has no containers")
    return body


def get_target_container_app(
    subscription_id,
    resource_group,
    target_app_name,
):
    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/containerApps/{target_app_name}"
    )
    client = ResourceManagementClient(DefaultAzureCredential(), subscription_id)
    try:
        return client.resources.get_by_id(
            resource_id,
            api_version=CONTAINER_APPS_API_VERSION,
        )
    except ResourceNotFoundError:
        return None
    except HttpResponseError as error:
        if error.status_code == 404:
            return None
        raise RuntimeError(
            f"Unable to check destination app availability: {error}"
        ) from error
def ensure_target_available(
    subscription_id,
    resource_group,
    target_app_name,
    source_app_id,
    allow_update=False,
):
    target = get_target_container_app(
        subscription_id,
        resource_group,
        target_app_name,
    )
    if target is None:
        return "missing"
    tags = target.tags or {}
    if tags.get("migrate-via-tool") == "true":
        if (
            tags.get("migration-source-id", "").lower()
            == source_app_id.lower()
        ):
            return "migrated"
        if not allow_update:
            return "tool-created"
    if not allow_update:
        raise ValueError(
            f"Destination app already exists: {resource_group}/{target_app_name}"
        )
    return "update"


def check_migrated_app_health(
    subscription_id,
    resource_group,
    target_app_name,
    source_app_id,
):
    target = get_target_container_app(
        subscription_id,
        resource_group,
        target_app_name,
    )
    target_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.App/containerApps/{target_app_name}"
    )
    if target is None:
        return {
            "target_id": target_id,
            "healthy": False,
            "checkpoint_match": False,
            "checks": {"exists": False},
        }

    properties = target.properties or {}
    configuration = properties.get("configuration") or {}
    template = properties.get("template") or {}
    tags = target.tags or {}
    checks = {
        "exists": True,
        "provisioning_succeeded": (
            properties.get("provisioningState") == "Succeeded"
        ),
        "running": properties.get("runningStatus") == "Running",
        "ready_revision": bool(properties.get("latestReadyRevisionName")),
        "has_containers": bool(template.get("containers")),
    }
    checkpoint_match = (
        tags.get("migrate-via-tool") == "true"
        and tags.get("migration-source-id", "").lower() == source_app_id.lower()
    )
    ingress = configuration.get("ingress") or {}
    return {
        "target_id": target_id,
        "healthy": all(checks.values()),
        "checkpoint_match": checkpoint_match,
        "checks": checks,
        "provisioning_state": properties.get("provisioningState"),
        "running_status": properties.get("runningStatus"),
        "latest_ready_revision": properties.get("latestReadyRevisionName"),
        "fqdn": ingress.get("fqdn"),
    }


def print_health_report(report):
    print("\nMigrated app health")
    print("-" * 80)
    print(f"Target ARM ID:       {report['target_id']}")
    print(f"Resource health:     {'Healthy' if report['healthy'] else 'Unhealthy'}")
    print(
        f"Migration checkpoint: "
        f"{'Matched' if report['checkpoint_match'] else 'Missing or mismatched'}"
    )
    for name, passed in report["checks"].items():
        print(f"  {name.replace('_', ' ').title()}: {'Pass' if passed else 'Fail'}")
    if report.get("provisioning_state"):
        print(f"Provisioning state:  {report['provisioning_state']}")
    if report.get("running_status"):
        print(f"Running status:      {report['running_status']}")
    if report.get("latest_ready_revision"):
        print(f"Ready revision:      {report['latest_ready_revision']}")
    if report.get("fqdn"):
        print(f"FQDN:                {report['fqdn']}")
    print("-" * 80)


def wait_for_migrated_app_health(
    subscription_id,
    resource_group,
    target_app_name,
    source_app_id,
    attempts=12,
    interval_seconds=10,
):
    report = None
    for attempt in range(attempts):
        report = check_migrated_app_health(
            subscription_id,
            resource_group,
            target_app_name,
            source_app_id,
        )
        if report["healthy"] and report["checkpoint_match"]:
            return report
        if attempt + 1 < attempts:
            time.sleep(interval_seconds)
    return report


def append_migration_checkpoint(
    checkpoint_csv,
    subscription_id,
    source_resource_group,
    source_app_name,
    target_resource_group,
    target_app_name,
    environment_id,
):
    path = Path(checkpoint_csv).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    source_app_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{source_resource_group}"
        f"/providers/Microsoft.Web/sites/{source_app_name}"
    )
    target_app_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{target_resource_group}"
        f"/providers/Microsoft.App/containerApps/{target_app_name}"
    )
    row = {
        "migrated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source_app_id": source_app_id,
        "target_app_id": target_app_id,
        "subscription_id": subscription_id,
        "source_resource_group": source_resource_group,
        "source_app_name": source_app_name,
        "target_resource_group": target_resource_group,
        "target_app_name": target_app_name,
        "environment_id": environment_id,
    }
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CHECKPOINT_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow(row)
    print(f"Migration checkpoint: {path}")
    return path


def check_checkpoint_csv(checkpoint_csv):
    from inventory import print_table

    path = Path(checkpoint_csv).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve()
    if not path.exists():
        raise ValueError(f"Checkpoint CSV not found: {path}")

    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        raise ValueError(f"Checkpoint CSV is empty: {path}")

    reports = []
    for row in rows:
        reports.append(
            check_migrated_app_health(
                row["subscription_id"],
                row["target_resource_group"],
                row["target_app_name"],
                row["source_app_id"],
            )
        )

    print(f"\nMigration checkpoint health: {path}")
    values = [
        ("Total targets:", len(reports)),
        ("Healthy:", sum(report["healthy"] for report in reports)),
        ("Unhealthy:", sum(not report["healthy"] for report in reports)),
        (
            "Checkpoint mismatch:",
            sum(not report["checkpoint_match"] for report in reports),
        ),
    ]
    width = max(len(label) for label, _ in values)
    for label, value in values:
        print(f"  {label:<{width}} {value}")
    print()
    table_rows = []
    statuses = []
    for row, report in zip(rows, reports):
        table_rows.append(
            [
                row["target_resource_group"],
                row["target_app_name"],
                "Healthy" if report["healthy"] else "Unhealthy",
                "Matched" if report["checkpoint_match"] else "Missing or mismatched",
                report.get("provisioning_state") or "Unavailable",
                report.get("running_status") or "Unavailable",
            ]
        )
        statuses.append(
            "Ready"
            if report["healthy"] and report["checkpoint_match"]
            else "Blocked"
        )
    print_table(
        [
            "Resource Group",
            "Target App",
            "Health",
            "Checkpoint",
            "Provisioning",
            "Running",
        ],
        table_rows,
        max_widths=[40, 32, 10, 24, 14, 12],
        statuses=statuses,
        color=sys.stdout.isatty(),
    )
    return all(
        report["healthy"] and report["checkpoint_match"]
        for report in reports
    )


def deploy_v2_function_app(
    subscription_id,
    resource_group,
    target_app_name,
    v2_metadata,
    environment_id,
):
    body = build_container_app_body(
        v2_metadata,
        environment_id,
    )
    client = ResourceManagementClient(DefaultAzureCredential(), subscription_id)
    try:
        result = client.resources.begin_create_or_update(
            resource_group_name=resource_group,
            resource_provider_namespace="Microsoft.App",
            parent_resource_path="",
            resource_type="containerApps",
            resource_name=target_app_name,
            api_version=CONTAINER_APPS_API_VERSION,
            parameters=body,
        ).result()
    except Exception as error:
        raise RuntimeError(f"Container App deployment failed: {error}") from error
    print(f"v2 Function App deployed: {result.id}")
    print()
    return result


def _prompt_value(label, default=None):
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("A value is required.")


def _prompt_yes_no(label, default=False):
    suffix = " [Y/n]" if default else " [y/N]"
    value = input(f"{label}{suffix}: ").strip().lower()
    return default if not value else value in {"y", "yes"}


def _choose_interactive_source(subscription_id=None, resource_group=None, app_name=None):
    from inventory import (
        list_accessible_subscription_ids,
        list_v1_function_apps,
        print_inventory,
    )

    subscriptions = (
        [subscription_id]
        if subscription_id
        else list_accessible_subscription_ids()
    )
    apps = list_v1_function_apps(subscriptions, resource_group=resource_group)
    if app_name:
        apps = [app for app in apps if app["name"].lower() == app_name.lower()]
    if not apps:
        raise ValueError("No matching v1 Function Apps were found")

    print("\nSource app inventory\n")
    print_inventory(
        apps,
        columns=[
            "subscription-id",
            "resource-group",
            "app-name",
            "location",
            "migration-status",
            "destination-app",
        ],
    )
    ready = [app for app in apps if app["migration_status"] == "Ready"]
    if not ready:
        raise ValueError("All matching apps are blocked from migration")
    if len(ready) == 1:
        print(f"Selected source app: {ready[0]['resource_group']}/{ready[0]['name']}\n")
        return ready[0]

    print("Select a source app")
    print("-" * 80)
    for index, app in enumerate(ready, start=1):
        print(
            f"  [{index}] {app['subscription_name']} | "
            f"{app['resource_group']}/{app['name']} | {app['location']}"
        )
    print()
    while True:
        selection = _prompt_value("Source app number")
        if selection.isdigit() and 1 <= int(selection) <= len(ready):
            selected = ready[int(selection) - 1]
            print(
                f"\nSelected source app: "
                f"{selected['resource_group']}/{selected['name']}\n"
            )
            return selected
        print(f"Enter a number between 1 and {len(ready)}.")


def _print_migration_plan(
    subscription_id,
    source_rg,
    source_app,
    target_app,
    environment_id,
    v2_metadata,
):
    deployment = v2_metadata["deployment_properties"]
    resources = deployment["resources"]
    scale = deployment["scale"]
    source_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{source_rg}"
        f"/providers/Microsoft.Web/sites/{source_app}"
    )
    destination_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{source_rg}"
        f"/providers/Microsoft.App/containerApps/{target_app}"
    )
    print("\nMigration plan")
    print("-" * 80)
    print(f"Source ARM ID:      {source_id}")
    print(f"Destination ARM ID: {destination_id}")
    print(f"Environment: {environment_id}")
    print(f"Image:       {deployment['container_image']}")
    print(f"Resources:   {resources.get('cpu')} CPU, {resources.get('memory')} memory")
    print(
        f"Scale:       {scale.get('minReplicas')} to "
        f"{scale.get('maxReplicas')} replicas"
    )
    print(f"Dapr:        {'enabled' if deployment.get('dapr') else 'disabled'}")
    print("-" * 80)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate a legacy Functions proxy app to native Container Apps",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python migrate_function_app.py --interactive \\
    --source-subscription-id <SUBSCRIPTION_ID>

  python migrate_function_app.py \\
    --source-subscription-id <SUBSCRIPTION_ID> \\
    --source-rg <RESOURCE_GROUP> \\
    --source-app <FUNCTION_APP> \\
    --target-app <TARGET_APP> \\
    --confirm

  python migrate_function_app.py \\
    --source-app-id "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Web/sites/<FUNCTION_APP>" \\
    --dry-run

  python migrate_function_app.py \\
    --source-app-id "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Web/sites/<FUNCTION_APP>" \\
    --target-app <TARGET_APP> \\
    --check-health

  python migrate_function_app.py --check-health \\
    --checkpoint-csv migration-checkpoints.csv

  python migrate_function_app.py \\
    --source-subscription-id <SUBSCRIPTION_ID> \\
    --source-rg <RESOURCE_GROUP> \\
    --source-app <FUNCTION_APP> \\
    --dry-run
""",
    )
    parser.add_argument(
        "--source-subscription-id",
        help="Source Azure subscription ID; required unless supplied by --source-app-id or interactive selection",
    )
    parser.add_argument(
        "--source-rg",
        help="Source Function App resource group; required unless supplied by --source-app-id or interactive selection",
    )
    parser.add_argument(
        "--source-app",
        help="Source legacy Function App name; required unless supplied by --source-app-id or interactive selection",
    )
    parser.add_argument(
        "--source-app-id",
        help="Full Microsoft.Web/sites ARM resource ID used to derive source subscription, resource group, and app name (default: none)",
    )
    parser.add_argument(
        "--target-app",
        help="Destination native Container App name (default: generated from source name with -v2 suffix)",
    )
    parser.add_argument(
        "--system-identity-action",
        choices=["create", "skip"],
        help="Create a new target system identity or skip it when the backing app uses system identity (default: prompt interactively; otherwise required)",
    )
    parser.add_argument(
        "--allow-update-existing",
        action="store_true",
        help="Allow updating an existing destination Container App (default: false)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="List eligible apps, collect required input, preview the plan, and ask for confirmation (default: false)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Approve and execute a reviewed non-interactive deployment (default: false; required outside --interactive)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate the live migration plan without deployment (default: false)",
    )
    parser.add_argument(
        "--check-health",
        action="store_true",
        help="Check an existing migrated target's live health and migration checkpoint without changes (default: false)",
    )
    parser.add_argument(
        "--checkpoint-csv",
        default="migration-checkpoints.csv",
        help="Append successful migrations and provide batch health-check input (default: migration-checkpoints.csv in current directory)",
    )
    args = parser.parse_args()

    if args.check_health and not any(
        (
            args.source_subscription_id,
            args.source_rg,
            args.source_app,
            args.source_app_id,
        )
    ):
        try:
            healthy = check_checkpoint_csv(args.checkpoint_csv)
        except ValueError as error:
            parser.error(str(error))
        raise SystemExit(0 if healthy else 1)

    source_id_parts = _parse_source_app_id(args.source_app_id)
    if args.source_app_id and (
        source_id_parts.get("provider", "").lower() != "microsoft.web"
        or source_id_parts.get("resource_type", "").lower() != "sites"
    ):
        parser.error(
            "--source-app-id must identify a Microsoft.Web/sites resource"
        )
    source_subscription_id = (
        args.source_subscription_id or source_id_parts.get("subscription_id")
    )
    source_rg = args.source_rg or source_id_parts.get("resource_group")
    source_app = args.source_app or source_id_parts.get("resource_name")

    if args.interactive:
        if not sys.stdin.isatty():
            parser.error("--interactive requires a TTY")
        selected = _choose_interactive_source(
            source_subscription_id,
            source_rg,
            source_app,
        )
        source_subscription_id = selected["subscription_id"]
        source_rg = selected["resource_group"]
        source_app = selected["name"]

    missing = [
        name
        for name, value in (
            ("--source-subscription-id", source_subscription_id),
            ("--source-rg", source_rg),
            ("--source-app", source_app),
        )
        if not value
    ]
    if missing:
        parser.error("Missing required inputs: " + ", ".join(missing))

    target_app = args.target_app or default_target_app_name(source_app)
    if args.interactive:
        print("Destination")
        print("-" * 80)
        print(f"  Subscription:  {source_subscription_id}")
        print(f"  Resource group: {source_rg}")
        print()
        target_app = _prompt_value("Destination app name", target_app)
        print()
    name_error = validate_container_app_name(target_app)
    if name_error:
        parser.error(f"Invalid destination app name: {name_error}")

    source_app_id = (
        f"/subscriptions/{source_subscription_id}/resourceGroups/{source_rg}"
        f"/providers/Microsoft.Web/sites/{source_app}"
    )
    if args.check_health:
        report = check_migrated_app_health(
            source_subscription_id,
            source_rg,
            target_app,
            source_app_id,
        )
        print_health_report(report)
        raise SystemExit(0 if report["healthy"] else 1)

    while True:
        try:
            target_state = ensure_target_available(
                source_subscription_id,
                source_rg,
                target_app,
                source_app_id,
                allow_update=args.allow_update_existing,
            )
            if target_state in {"migrated", "tool-created"}:
                target_id = (
                    f"/subscriptions/{source_subscription_id}/"
                    f"resourceGroups/{source_rg}/providers/"
                    f"Microsoft.App/containerApps/{target_app}"
                )
                if not args.interactive:
                    if target_state == "migrated":
                        print(f"Migration already completed: {target_id}")
                        return
                    parser.error(
                        f"Destination appears created by this tool but its "
                        f"source checkpoint is missing or different: {target_id}. "
                        "Use --interactive to skip or choose a new target."
                    )

                checkpoint_text = (
                    "matches this source Function App"
                    if target_state == "migrated"
                    else "has migrate-via-tool=true but no matching source checkpoint"
                )
                print("\nExisting tool-created destination found")
                print(f"  ARM ID: {target_id}")
                print(f"  Checkpoint: {checkpoint_text}")
                print()
                while True:
                    choice = input(
                        "Choose [s]kip migration or create a [n]ew app: "
                    ).strip().lower()
                    if choice in {"s", "skip"}:
                        print("Migration skipped.")
                        return
                    if choice in {"n", "new"}:
                        target_app = _prompt_value(
                            "New destination app name",
                            default_target_app_name(source_app),
                        )
                        name_error = validate_container_app_name(target_app)
                        if name_error:
                            print(
                                f"Invalid destination app name: {name_error}\n"
                            )
                            continue
                        print()
                        break
                    print("Enter 's' to skip or 'n' to create a new app.")
                continue
            break
        except ValueError as error:
            if not args.interactive:
                parser.error(str(error))
            print(f"Destination unavailable: {error}\n")
            target_app = _prompt_value(
                "Choose another destination app name",
                default_target_app_name(source_app),
            )
            name_error = validate_container_app_name(target_app)
            if name_error:
                print(f"Invalid destination app name: {name_error}\n")
                continue
        except RuntimeError as error:
            parser.error(str(error))

    try:
        source = export_v1_metadata(
            source_subscription_id,
            source_rg,
            source_app,
        )
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))

    backing = source["properties"]["backing_container_app"]
    backing_identity = backing.get("identity")
    backing_configuration = backing.get("properties", {}).get("configuration", {})
    backing_registries = backing_configuration.get("registries") or []
    needs_system_identity = _identity_parts(backing_identity)[0] or any(
        str(registry.get("identity") or "").lower() == "system"
        for registry in backing_registries
    )
    system_action = args.system_identity_action
    if args.interactive and needs_system_identity and not system_action:
        system_action = (
            "create"
            if _prompt_yes_no("Create a new system identity on the v2 app?")
            else "skip"
        )

    environment_id = source["properties"]["managed_environment_id"]
    try:
        transformed = transform_to_v2(
            source,
            target_app,
            system_identity_action=system_action,
        )
        build_container_app_body(transformed, environment_id)
    except ValueError as error:
        parser.error(str(error))

    _print_migration_plan(
        source_subscription_id,
        source_rg,
        source_app,
        target_app,
        environment_id,
        transformed,
    )
    if args.dry_run:
        print("Dry-run complete. No Azure resources were changed.")
        return
    if args.interactive:
        if not _prompt_yes_no("Deploy this migration?"):
            print("Migration cancelled.")
            return
    elif not args.confirm:
        parser.error("Deployment requires --interactive or --confirm")

    deploy_v2_function_app(
        source_subscription_id,
        source_rg,
        target_app,
        transformed,
        environment_id,
    )
    append_migration_checkpoint(
        args.checkpoint_csv,
        source_subscription_id,
        source_rg,
        source_app,
        source_rg,
        target_app,
        environment_id,
    )
    health = wait_for_migrated_app_health(
        source_subscription_id,
        source_rg,
        target_app,
        source_app_id,
    )
    print_health_report(health)
    if not health["healthy"] or not health["checkpoint_match"]:
        raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        raise SystemExit(130)
