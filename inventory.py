#!/usr/bin/env python3
"""Inventory legacy Azure Function Apps hosted as Microsoft.Web/sites."""

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import sys
import textwrap
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest, QueryRequestOptions
from azure.mgmt.resource.subscriptions import SubscriptionClient

DEFAULT_COLUMNS = [
    "subscription-id",
    "resource-group",
    "app-name",
    "location",
]

COLUMN_DEFINITIONS = {
    "subscription-id": ("Subscription ID", "subscription_id"),
    "subscription-name": ("Subscription Name", "subscription_name"),
    "resource-group": ("Resource Group", "resource_group"),
    "app-name": ("App Name", "name"),
    "location": ("Location", "location"),
    "created-at": ("Created At", "created_at"),
    "last-modified-at": ("Last Modified At", "last_modified_at"),
    "managed-resource-group": ("Managed Resource Group", "managed_resource_group"),
    "backing-app": (
        "Backing Container App Name",
        "backing_container_app_name",
    ),
    "environment-name": (
        "Linked Container App Environment Name",
        "container_app_environment_name",
    ),
    "environment-resource-group": (
        "Linked Container App Environment RG",
        "container_app_environment_resource_group",
    ),
    "function-app-id": ("Function App ARM ID", "function_app_id"),
    "backing-app-id": (
        "Backing Container App ARM ID",
        "backing_container_app_id",
    ),
    "environment-id": (
        "Container App Environment ARM ID",
        "container_app_environment_id",
    ),
    "destination-app": ("Destination App", "destination_app_name"),
    "destination-app-id": ("Destination App ARM ID", "destination_app_id"),
    "migration-status": ("Migration Status", "migration_status"),
    "migration-blocker": ("Migration Blocker", "migration_blocker"),
    "migration-input": ("Migration Input Required", "migration_input_required"),
}


def default_target_app_name(source_name):
    normalized = re.sub(r"[^a-z0-9-]", "-", source_name.lower())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    if not normalized or not normalized[0].isalpha():
        normalized = f"f-{normalized}"
    if len(normalized) <= 29:
        return f"{normalized}-v2"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:6]
    return f"{normalized[:22].rstrip('-')}-{digest}-v2"


def _resource_id_value(resource_id, segment):
    if not resource_id:
        return None
    parts = resource_id.split("/")
    for index, part in enumerate(parts):
        if part.lower() == segment.lower() and index + 1 < len(parts):
            return parts[index + 1]
    return None


def list_v1_function_apps(subscription_ids, resource_group=None, credential=None):
    """Query legacy Function Apps across one or more subscriptions."""
    credential = credential or DefaultAzureCredential()
    if isinstance(subscription_ids, str):
        subscription_ids = [subscription_ids]
    if not subscription_ids:
        return []

    resource_group_filter = ""
    if resource_group:
        escaped_resource_group = resource_group.replace("'", "''")
        resource_group_filter = (
            f"\n| where resourceGroup =~ '{escaped_resource_group}'"
        )
    query = (
        "Resources\n"
        "| where type =~ 'microsoft.web/sites'\n"
        "| where kind contains 'functionapp'\n"
        "| where kind contains 'azurecontainerapps'"
        f"{resource_group_filter}\n"
        "| extend siteIdLower=tolower(id)\n"
        "| join kind=leftouter (\n"
        "    Resources\n"
        "    | where type =~ 'microsoft.app/containerapps'\n"
        "    | where isnotempty(managedBy)\n"
        "    | extend siteIdLower=tolower(tostring(managedBy))\n"
        "    | project siteIdLower, "
        "backingManagedResourceGroup=resourceGroup, "
        "backingContainerApp=name, "
        "backingContainerAppId=id, "
        "backingImage=tostring(properties.template.containers[0].image), "
        "backingProvisioningState=tostring(properties.provisioningState), "
        "backingIdentityType=tostring(identity.type), "
        "backingRegistries=tostring(properties.configuration.registries), "
        "backingCreatedTime=tostring(systemData.createdAt), "
        "backingLastModifiedTime=tostring(systemData.lastModifiedAt), "
        "backingEnvironmentId=tostring(properties.environmentId)\n"
        ") on siteIdLower\n"
        "| join kind=leftouter (\n"
        "    Resources\n"
        "    | where type =~ 'microsoft.app/containerapps'\n"
        "    | where tostring(tags['migrate-via-tool']) =~ 'true'\n"
        "    | extend siteIdLower=tolower(tostring(tags['migration-source-id']))\n"
        "    | where isnotempty(siteIdLower)\n"
        "    | summarize "
        "destinationAppName=take_any(name), "
        "destinationAppId=take_any(id), "
        "destinationResourceGroup=take_any(resourceGroup) "
        "by siteIdLower\n"
        ") on siteIdLower\n"
        "| join kind=leftouter (\n"
        "    ResourceContainers\n"
        "    | where type =~ 'microsoft.resources/subscriptions'\n"
        "    | project subscriptionId, subscriptionName=name\n"
        ") on subscriptionId\n"
        "| project subscriptionName, subscriptionId, resourceGroup, "
        "name, location, id, "
        "backingManagedResourceGroup, backingContainerApp, "
        "backingContainerAppId, backingImage, backingProvisioningState, "
        "backingIdentityType, backingRegistries, "
        "backingCreatedTime, "
        "backingLastModifiedTime, backingEnvironmentId, "
        "destinationAppName=tostring(destinationAppName), "
        "destinationAppId=tostring(destinationAppId), "
        "destinationResourceGroup=tostring(destinationResourceGroup)\n"
        "| order by subscriptionId asc, resourceGroup asc, name asc"
    )

    client = ResourceGraphClient(credential)
    apps = []
    skip_token = None
    while True:
        response = client.resources(
            QueryRequest(
                subscriptions=list(subscription_ids),
                query=query,
                options=QueryRequestOptions(
                    result_format="objectArray",
                    top=1000,
                    skip_token=skip_token,
                ),
            )
        )
        for resource in response.data or []:
            blockers = []
            if not resource.get("backingContainerAppId"):
                blockers.append("Backing Container App was not found")
            if not resource.get("backingEnvironmentId"):
                blockers.append("Linked Container Apps environment was not found")
            if not resource.get("backingImage"):
                blockers.append("Backing Container App image is missing")
            provisioning_state = resource.get("backingProvisioningState")
            if provisioning_state and provisioning_state.lower() != "succeeded":
                blockers.append(
                    f"Backing Container App provisioning state is {provisioning_state}"
                )
            needs_system_identity = (
                "systemassigned" in str(
                    resource.get("backingIdentityType") or ""
                ).lower().replace("_", "")
                or '"identity":"system"' in str(
                    resource.get("backingRegistries") or ""
                ).lower().replace(" ", "")
            )
            migration_input = (
                "Choose system_identity_action=create or skip"
                if needs_system_identity and not blockers
                else None
            )
            destination_app_id = resource.get("destinationAppId") or None
            if destination_app_id:
                migration_status = "Migrated"
                blockers = []
                migration_input = None
            elif blockers:
                migration_status = "Blocked"
            elif migration_input:
                migration_status = "Needs Input"
            else:
                migration_status = "Ready"
            apps.append(
                {
                    "subscription_name": resource.get("subscriptionName") or "",
                    "subscription_id": resource["subscriptionId"],
                    "resource_group": resource["resourceGroup"],
                    "name": resource["name"],
                    "location": resource["location"],
                    "function_app_id": resource["id"],
                    "created_at": resource.get("backingCreatedTime") or None,
                    "last_modified_at": resource.get(
                        "backingLastModifiedTime"
                    )
                    or None,
                    "managed_resource_group": resource.get(
                        "backingManagedResourceGroup"
                    )
                    or None,
                    "backing_container_app_name": resource.get(
                        "backingContainerApp"
                    )
                    or None,
                    "backing_container_app_id": resource.get(
                        "backingContainerAppId"
                    )
                    or None,
                    "container_app_environment_id": resource.get(
                        "backingEnvironmentId"
                    )
                    or None,
                    "container_app_environment_name": _resource_id_value(
                        resource.get("backingEnvironmentId"),
                        "managedEnvironments",
                    ),
                    "container_app_environment_resource_group": _resource_id_value(
                        resource.get("backingEnvironmentId"),
                        "resourceGroups",
                    ),
                    "destination_app_name": resource.get(
                        "destinationAppName"
                    )
                    or None,
                    "destination_app_id": destination_app_id,
                    "destination_resource_group": resource.get(
                        "destinationResourceGroup"
                    )
                    or None,
                    "migration_status": migration_status,
                    "migration_blocker": "; ".join(blockers) if blockers else None,
                    "migration_input_required": migration_input,
                }
            )
        skip_token = response.skip_token
        if not skip_token:
            break
    return apps


def list_accessible_subscription_ids(credential=None):
    """List enabled subscriptions visible to the current Azure identity."""
    credential = credential or DefaultAzureCredential()
    client = SubscriptionClient(credential)
    return [
        subscription.subscription_id
        for subscription in client.subscriptions.list()
        if str(getattr(subscription, "state", "")).lower().endswith("enabled")
    ]


def parse_columns(value):
    """Parse and validate comma-separated table column identifiers."""
    columns = [item.strip().lower() for item in value.split(",") if item.strip()]
    unknown = [column for column in columns if column not in COLUMN_DEFINITIONS]
    if unknown:
        raise ValueError(
            f"Unknown columns: {', '.join(unknown)}. Valid columns: "
            f"{', '.join(COLUMN_DEFINITIONS)}"
        )
    if not columns:
        raise ValueError("At least one table column is required")
    return columns


def _display_value(app, key):
    value = app.get(key)
    if key == "migration_blocker" and app.get("migration_status") != "Blocked":
        return "Not Applicable"
    if (
        key == "migration_input_required"
        and app.get("migration_status") != "Needs Input"
    ):
        return "Not Applicable"
    return str(value) if value not in {None, ""} else "Unavailable"


def _color_text(text, status, enabled):
    if not enabled:
        return text
    color = {
        "Ready": "\033[32m",
        "Migrated": "\033[32m",
        "Needs Input": "\033[33m",
        "Blocked": "\033[31m",
    }.get(status)
    return f"{color}{text}\033[0m" if color else text


def print_table(
    headers,
    rows,
    max_widths=None,
    indent="  ",
    statuses=None,
    color=False,
):
    """Render a bordered table and wrap long cells without losing content."""
    max_widths = max_widths or [60] * len(headers)
    widths = []
    for index, header in enumerate(headers):
        content_width = max(
            [len(header), *(len(str(row[index])) for row in rows)]
        )
        widths.append(min(content_width, max_widths[index]))

    def border(separator="-"):
        return indent + "+" + "+".join(
            separator * (width + 2) for width in widths
        ) + "+"

    def render_row(values, status=None):
        wrapped = [
            textwrap.wrap(
                str(value),
                width=widths[index],
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
            for index, value in enumerate(values)
        ]
        for line_index in range(max(len(lines) for lines in wrapped)):
            cells = [
                lines[line_index] if line_index < len(lines) else ""
                for lines in wrapped
            ]
            line = (
                indent
                + "| "
                + " | ".join(
                    f"{cell:<{widths[index]}}"
                    for index, cell in enumerate(cells)
                )
                + " |"
            )
            print(_color_text(line, status, color))

    print(border())
    render_row(headers)
    print(border())
    for index, row in enumerate(rows):
        status = statuses[index] if statuses else None
        render_row(row, status)
    print(border())


def print_inventory_summary(apps, indent="  ", color=False):
    blocked_count = sum(
        app.get("migration_status") == "Blocked" for app in apps
    )
    input_count = sum(
        app.get("migration_status") == "Needs Input" for app in apps
    )
    migrated_count = sum(
        app.get("migration_status") == "Migrated" for app in apps
    )
    values = [
        ("Total v1 apps:", len(apps)),
        (
            "Eligible for migration:",
            len(apps) - blocked_count - migrated_count,
        ),
        ("Customer input required:", input_count),
        ("Migration blocked:", blocked_count),
        ("Already migrated:", migrated_count),
    ]
    width = max(len(label) for label, _ in values)
    for label, value in values:
        print(f"{indent}{label:<{width}} {value}")


def print_inventory(apps, columns=None, color=None):
    """Print a concise v1 Function App inventory."""
    columns = columns or DEFAULT_COLUMNS
    color = (
        sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if color is None
        else color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    print("=" * 80)
    print("Legacy Azure Function Apps (Microsoft.Web/sites)")
    print("=" * 80)
    print()
    print_inventory_summary(apps, color=color)
    print()
    headers = [COLUMN_DEFINITIONS[column][0] for column in columns]
    rows = [
        [
            _display_value(app, COLUMN_DEFINITIONS[column][1])
            for column in columns
        ]
        for app in apps
    ]
    max_widths = [
        36 if column == "subscription-id"
        else 40 if column in {"resource-group", "managed-resource-group"}
        else 32 if column in {"app-name", "backing-app"}
        else 70 if column.endswith("-id")
        else 28
        for column in columns
    ]
    print_table(
        headers,
        rows,
        max_widths=max_widths,
        statuses=[app.get("migration_status") for app in apps],
        color=color,
    )
    print()
    print_migration_blockers(apps, color=color)
    print_migration_inputs(apps, color=color)
    print_migrated_apps(apps, color=color)


def print_migration_blockers(apps, color=False):
    """Show broken proxy setups that cannot be migrated."""
    blocked = [app for app in apps if app.get("migration_status") == "Blocked"]
    _print_issue_table(
        "Migration blockers",
        blocked,
        "migration_blocker",
        "Reason",
        color=color,
    )


def print_migration_inputs(apps, color=False):
    """Show migration-capable apps that require a customer decision."""
    pending = [
        app for app in apps if app.get("migration_status") == "Needs Input"
    ]
    _print_issue_table(
        "Customer input required",
        pending,
        "migration_input_required",
        "Required Input",
        color=color,
    )


def print_migrated_apps(apps, color=False):
    """Show source-to-destination mappings for completed migrations."""
    migrated = [
        app for app in apps if app.get("migration_status") == "Migrated"
    ]
    if not migrated:
        return
    headers = [
        "Subscription ID",
        "Resource Group",
        "Source App",
        "Destination App",
    ]
    rows = [
        [
            app["subscription_id"],
            app["resource_group"],
            app["name"],
            app.get("destination_app_name") or "Unavailable",
        ]
        for app in migrated
    ]
    print("Already migrated")
    print_table(
        headers,
        rows,
        max_widths=[36, 40, 32, 32],
        statuses=["Migrated"] * len(rows),
        color=color,
    )
    print()


def _print_issue_table(title, apps, reason_key, reason_header, color=False):
    """Render inventory problems with consistent indentation and wrapped details."""
    if not apps:
        return
    headers = ["Subscription ID", "Resource Group", "App Name", reason_header]
    keys = ["subscription_id", "resource_group", "name", reason_key]
    print(title)
    rows = [[str(app.get(key) or "") for key in keys] for app in apps]
    print_table(
        headers,
        rows,
        max_widths=[36, 40, 32, 60],
        statuses=[app.get("migration_status") for app in apps],
        color=color,
    )
    print()


def print_inventory_details(apps, color=None):
    """Print all inventory fields vertically, one app per block."""
    color = (
        sys.stdout.isatty() and "NO_COLOR" not in os.environ
        if color is None
        else color and sys.stdout.isatty() and "NO_COLOR" not in os.environ
    )
    print("=" * 80)
    print("Legacy Azure Function Apps - Full Details")
    print("=" * 80)
    print()
    print_inventory_summary(apps, indent="", color=color)
    for index, app in enumerate(apps, start=1):
        print(
            _color_text(
                f"\nApp {index} of {len(apps)}",
                app.get("migration_status"),
                color,
            )
        )
        print("-" * 80)
        for header, key in COLUMN_DEFINITIONS.values():
            print(f"{header}: {_display_value(app, key)}")
    print()


def export_csv(apps, output_file):
    """Export a v1 inventory as a bulk migration plan."""
    output_path = Path.cwd() / output_file
    if output_path.exists():
        suffix = 1
        while True:
            candidate = output_path.with_name(
                f"{output_path.stem}-{suffix}{output_path.suffix}"
            )
            if not candidate.exists():
                output_path = candidate
                break
            suffix += 1
    output_path = output_path.resolve()
    fieldnames = [
        "source_app_name",
        "source_subscription_name",
        "source_resource_group",
        "source_subscription_id",
        "source_location",
        "source_created_at",
        "source_last_modified_at",
        "source_managed_resource_group",
        "source_backing_container_app_name",
        "source_linked_container_app_environment_name",
        "source_linked_container_app_environment_resource_group",
        "source_function_app_id",
        "source_backing_container_app_id",
        "source_linked_container_app_environment_id",
        "migration_status",
        "migration_blocker",
        "migration_input_required",
        "target_resource_group",
        "target_app_name",
        "system_identity_action",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for app in apps:
            writer.writerow(
                {
                    "source_app_name": app["name"],
                    "source_subscription_name": app["subscription_name"],
                    "source_resource_group": app["resource_group"],
                    "source_subscription_id": app["subscription_id"],
                    "source_location": app["location"],
                    "source_created_at": app.get("created_at") or "",
                    "source_last_modified_at": app.get("last_modified_at") or "",
                    "source_managed_resource_group": app.get(
                        "managed_resource_group"
                    )
                    or "",
                    "source_backing_container_app_name": app.get(
                        "backing_container_app_name"
                    )
                    or "",
                    "source_linked_container_app_environment_name": app.get(
                        "container_app_environment_name"
                    )
                    or "",
                    "source_linked_container_app_environment_resource_group": app.get(
                        "container_app_environment_resource_group"
                    )
                    or "",
                    "source_function_app_id": app.get("function_app_id") or "",
                    "source_backing_container_app_id": app.get(
                        "backing_container_app_id"
                    )
                    or "",
                    "source_linked_container_app_environment_id": app.get(
                        "container_app_environment_id"
                    )
                    or "",
                    "migration_status": app.get("migration_status") or "Blocked",
                    "migration_blocker": (
                        app.get("migration_blocker")
                        or "Not Applicable"
                    ),
                    "migration_input_required": (
                        app.get("migration_input_required")
                        or "Not Applicable"
                    ),
                    "target_resource_group": (
                        app.get("destination_resource_group")
                        or app["resource_group"]
                    ),
                    "target_app_name": (
                        app.get("destination_app_name")
                        or default_target_app_name(app["name"])
                    ),
                    "system_identity_action": "",
                }
            )

    print(f"Exported {len(apps)} v1 apps")
    print(f"CSV file: {output_path}")
    print("Complete the target columns, then run:")
    print(f"  python bulk_migrate.py --input-csv \"{output_path}\" --dry-run")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description=(
            "List legacy Functions-on-Container-Apps v1 resources "
            "(Microsoft.Web/sites with kind containing azurecontainerapps)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python inventory.py --subscription-id <SUBSCRIPTION_ID>
  python inventory.py --all-subscriptions
  python inventory.py --all-subscriptions --json
  python inventory.py --all-subscriptions --export-csv
  python inventory.py --all-subscriptions --all-columns
  python inventory.py --all-subscriptions \\
    --columns subscription-id,resource-group,app-name,migration-status
""",
    )
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--subscription-id",
        help="Scan one Azure subscription ID; mutually exclusive with --all-subscriptions",
    )
    scope.add_argument(
        "--all-subscriptions",
        action="store_true",
        help="Scan all enabled accessible subscriptions (default: false; mutually exclusive with --subscription-id)",
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--export-csv",
        nargs="?",
        const="",
        metavar="FILE",
        help=(
            "Export a bulk migration plan in the current directory. "
            "Default: timestamped filename"
        ),
    )
    output_group.add_argument(
        "--json",
        action="store_true",
        help="Write color-free JSON to stdout instead of a table (default: false)",
    )
    table_columns = parser.add_mutually_exclusive_group()
    table_columns.add_argument(
        "--columns",
        help=(
            "Comma-separated table columns. Valid values: "
            + ", ".join(COLUMN_DEFINITIONS)
            + " (default: subscription-id,resource-group,app-name,location)"
        ),
    )
    table_columns.add_argument(
        "--all-columns",
        action="store_true",
        help="Show every available field in a vertical detail view (default: false)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI status colors in TTY table output (default: false)",
    )
    args = parser.parse_args()

    credential = DefaultAzureCredential()
    subscription_ids = (
        list_accessible_subscription_ids(credential)
        if args.all_subscriptions
        else [args.subscription_id]
    )
    if not args.json:
        scope_text = (
            f"{len(subscription_ids)} accessible subscriptions"
            if args.all_subscriptions
            else f"subscription {args.subscription_id}"
        )
        print(f"\nScanning {scope_text} for v1 Function Apps...")
    try:
        apps = list_v1_function_apps(
            subscription_ids,
            credential=credential,
        )
    except Exception as e:
        print(f"Error: inventory query failed: {e}", file=sys.stderr)
        raise SystemExit(1)

    if args.json:
        blocked_count = sum(
            app.get("migration_status") == "Blocked" for app in apps
        )
        input_count = sum(
            app.get("migration_status") == "Needs Input" for app in apps
        )
        migrated_count = sum(
            app.get("migration_status") == "Migrated" for app in apps
        )
        print(
            json.dumps(
                {
                    "summary": {
                        "subscriptions_scanned": len(subscription_ids),
                        "total_v1_apps": len(apps),
                        "eligible_for_migration": (
                            len(apps) - blocked_count - migrated_count
                        ),
                        "customer_input_required": input_count,
                        "migration_blocked": blocked_count,
                        "already_migrated": migrated_count,
                    },
                    "v1_apps": apps,
                },
                indent=2,
            )
        )
    elif args.export_csv is not None:
        output_file = args.export_csv
        if output_file:
            if Path(output_file).name != output_file:
                parser.error("--export-csv accepts a filename, not a path")
        else:
            timestamp = datetime.datetime.now(
                datetime.timezone.utc
            ).strftime("%Y%m%d-%H%M%S")
            output_file = f"function-app-v1-migration-plan-{timestamp}.csv"
        export_csv(apps, output_file)
    else:
        try:
            columns = parse_columns(args.columns) if args.columns else DEFAULT_COLUMNS
        except ValueError as e:
            parser.error(str(e))
        if args.all_columns:
            print_inventory_details(apps, color=not args.no_color)
        else:
            print_inventory(apps, columns, color=not args.no_color)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        raise SystemExit(130)
