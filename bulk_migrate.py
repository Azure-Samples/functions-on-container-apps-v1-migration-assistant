#!/usr/bin/env python3
"""
Bulk migration from CSV input.

Reads a CSV file (exported by inventory.py --export-csv) with source/target
mappings and runs migrations sequentially, tracking progress.

Usage:
    python bulk_migrate.py --input-csv inventory.csv
    python bulk_migrate.py --input-csv inventory.csv --dry-run
    python bulk_migrate.py --input-csv inventory.csv --include-migrated
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from migrate_function_app import (
    append_migration_checkpoint,
    export_v1_metadata,
    transform_to_v2,
    deploy_v2_function_app,
    build_container_app_body,
    default_target_app_name,
    ensure_target_available,
    validate_container_app_name,
    wait_for_migrated_app_health,
)


def load_csv(input_file):
    """Load migration plan from CSV."""
    rows = []
    with open(input_file, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def checkpoint_csv_for_input(input_csv):
    input_stem = Path(input_csv).expanduser().stem
    return (Path.cwd() / f"{input_stem}-checkpoints.csv").resolve()


def results_json_for_input(input_csv):
    input_stem = Path(input_csv).expanduser().stem
    return (Path.cwd() / f"{input_stem}-results.json").resolve()


def validate_csv(rows):
    """Validate CSV has required fields for migration."""
    required = ["source_app_name", "source_resource_group", "source_subscription_id"]
    target_required = ["target_resource_group", "target_app_name"]
    errors = []

    for i, row in enumerate(rows, start=2):  # row 1 is header
        for field in required:
            if not row.get(field, "").strip():
                errors.append(f"Row {i}: missing {field}")

        # Target fields required for non-migrated rows
        status = row.get("migration_status", "").strip().lower()
        if status == "blocked":
            continue
        if status != "migrated":
            for field in target_required:
                if not row.get(field, "").strip():
                    errors.append(f"Row {i}: missing {field} (required for pending migration)")
            target_name = row.get("target_app_name", "").strip()
            if target_name:
                name_error = validate_container_app_name(target_name)
                if name_error:
                    errors.append(
                        f"Row {i}: invalid target_app_name '{target_name}': "
                        f"{name_error}"
                    )
            identity_action = row.get("system_identity_action", "").strip().lower()
            if identity_action and identity_action not in {"create", "skip"}:
                errors.append(
                    f"Row {i}: system_identity_action must be 'create' or 'skip'"
                )

    return errors


def print_progress(completed, total, current_app, status="in-progress"):
    """Print a progress line."""
    pct = (completed / total * 100) if total > 0 else 0
    bar_width = 30
    filled = int(bar_width * pct / 100)
    bar = "█" * filled + "░" * (bar_width - filled)

    status_icon = {"in-progress": "⟳", "success": "✓", "failed": "✗", "skipped": "→"}.get(status, " ")
    print(f"\r  [{bar}] {pct:5.1f}% ({completed}/{total}) {status_icon} {current_app}", end="", flush=True)


def run_bulk_migration(
    rows,
    dry_run=False,
    skip_migrated=True,
    results_file="bulk_migration_results.json",
    skip_confirmation=False,
    allow_update_existing=False,
    checkpoint_csv="migration-checkpoints.csv",
    default_system_identity_action=None,
):
    """Run migration for each row in the CSV."""
    total = len(rows)
    blocked = [
        row
        for row in rows
        if row.get("migration_status", "").strip().lower() == "blocked"
    ]
    needs_input = [
        row
        for row in rows
        if row.get("migration_status", "").strip().lower() == "needs input"
        and not row.get("system_identity_action", "").strip()
        and not default_system_identity_action
    ]
    pending = [
        row
        for row in rows
        if row.get("migration_status", "").strip().lower() != "blocked"
        and not (
            row.get("migration_status", "").strip().lower() == "needs input"
            and not row.get("system_identity_action", "").strip()
            and not default_system_identity_action
        )
        and (
            row.get("migration_status", "").strip().lower() != "migrated"
            or not skip_migrated
        )
    ]
    skipped = total - len(pending) - len(blocked) - len(needs_input)

    print("=" * 70)
    print("Bulk Migration — Azure Function App v1 → v2")
    print("=" * 70)
    print(f"\n  Total in CSV: {total}")
    print(f"  Already migrated (skipped): {skipped}")
    print(f"  Broken source setups (blocked): {len(blocked)}")
    print(f"  Customer input required: {len(needs_input)}")
    print(f"  To migrate: {len(pending)}")
    if dry_run:
        print(f"  Mode: DRY RUN (no deployments)")
    print()

    if blocked:
        print("  Migration blockers:")
        for row in blocked:
            reason = (
                row.get("migration_blocker", "").strip()
                or "Source setup is incomplete"
            )
            print(
                f"    {row.get('source_subscription_id', '').strip()}/"
                f"{row.get('source_resource_group', '').strip()}/"
                f"{row.get('source_app_name', '').strip()}: {reason}"
            )
        print()

    if needs_input:
        print("  Customer input required:")
        headers = ["Subscription ID", "Resource Group", "App Name", "Required Input"]
        keys = [
            "source_subscription_id",
            "source_resource_group",
            "source_app_name",
            "migration_input_required",
        ]
        widths = {
            key: max(
                len(header),
                *(len(str(row.get(key) or "")) for row in needs_input),
            )
            for header, key in zip(headers, keys)
        }
        print(
            "    "
            + " ".join(
                f"{header:<{widths[key]}}"
                for header, key in zip(headers, keys)
            )
        )
        print("    " + " ".join("-" * widths[key] for key in keys))
        for row in needs_input:
            print(
                "    "
                + " ".join(
                    f"{str(row.get(key) or ''):<{widths[key]}}"
                    for key in keys
                )
            )
        print()

    if not dry_run and pending:
        print("  Planned migrations:")
        for row in pending:
            target_rg = row.get("target_resource_group", "").strip()
            target_app = row.get("target_app_name", "").strip()
            print(
                f"    {row['source_subscription_id'].strip()}/"
                f"{row['source_resource_group'].strip()}/"
                f"{row['source_app_name'].strip()} -> "
                f"{target_rg}/{target_app} (same managed environment)"
            )
        if not skip_confirmation:
            if not sys.stdin.isatty():
                print("Error: non-interactive deployment requires --confirm")
                return 1
            response = input("\nDeploy all planned migrations? [y/N]: ").strip().lower()
            if response not in {"y", "yes"}:
                print("Bulk migration cancelled.")
                return 1

    results = [
        {
            "source": row.get("source_app_name", "").strip(),
            "target": row.get("target_app_name", "").strip(),
            "status": "blocked",
            "message": (
                row.get("migration_blocker", "").strip()
                or "Source setup is incomplete"
            ),
        }
        for row in blocked
    ]
    results.extend(
        {
            "source": row.get("source_app_name", "").strip(),
            "target": row.get("target_app_name", "").strip(),
            "status": "needs-input",
            "message": (
                row.get("migration_input_required", "").strip()
                or "Complete the required migration decision fields"
            ),
        }
        for row in needs_input
    )
    completed = 0

    for row in pending:
        source_app = row["source_app_name"].strip()
        source_rg = row["source_resource_group"].strip()
        source_sub = row["source_subscription_id"].strip()
        target_sub = source_sub
        target_rg = row.get("target_resource_group", "").strip() or source_rg
        target_app = (
            row.get("target_app_name", "").strip()
            or default_target_app_name(source_app)
        )
        system_identity_action = (
            row.get("system_identity_action", "").strip().lower()
            or default_system_identity_action
        )

        print_progress(completed, len(pending), source_app, "in-progress")

        try:
            name_error = validate_container_app_name(target_app)
            if name_error:
                raise ValueError(
                    f"Invalid destination app name '{target_app}': {name_error}"
                )
            target_state = ensure_target_available(
                source_sub,
                target_rg,
                target_app,
                (
                    f"/subscriptions/{source_sub}/resourceGroups/{source_rg}/"
                    f"providers/Microsoft.Web/sites/{source_app}"
                ),
                allow_update=allow_update_existing,
            )
            if target_state == "migrated":
                results.append({
                    "source": source_app,
                    "target": target_app,
                    "status": "already-migrated",
                    "message": "Tagged destination Container App already exists",
                })
                completed += 1
                print_progress(completed, len(pending), source_app, "skipped")
                print()
                continue
            if target_state == "tool-created":
                results.append({
                    "source": source_app,
                    "target": target_app,
                    "status": "needs-input",
                    "message": (
                        "Destination has migrate-via-tool=true but its source "
                        "checkpoint is missing or different; choose a new target "
                        "name or explicitly allow update"
                    ),
                })
                completed += 1
                print_progress(completed, len(pending), source_app, "skipped")
                print()
                continue

            v1_metadata = export_v1_metadata(source_sub, source_rg, source_app)
            environment_id = v1_metadata.get("properties", {}).get(
                "managed_environment_id"
            )
            if not environment_id:
                raise ValueError("Source app does not expose managedEnvironmentId")

            v2_metadata = transform_to_v2(
                v1_metadata,
                target_app,
                system_identity_action=system_identity_action,
            )
            build_container_app_body(v2_metadata, environment_id)

            if dry_run:
                results.append({
                    "source": source_app,
                    "target": target_app,
                    "status": "dry-run",
                    "message": "Source metadata and v2 request validated",
                })
                completed += 1
                print_progress(completed, len(pending), source_app, "skipped")
                print()
                continue

            deploy_v2_function_app(
                target_sub,
                target_rg,
                target_app,
                v2_metadata,
                environment_id,
            )
            append_migration_checkpoint(
                checkpoint_csv,
                source_sub,
                source_rg,
                source_app,
                target_rg,
                target_app,
                environment_id,
            )
            source_app_id = (
                f"/subscriptions/{source_sub}/resourceGroups/{source_rg}/"
                f"providers/Microsoft.Web/sites/{source_app}"
            )
            health = wait_for_migrated_app_health(
                source_sub,
                target_rg,
                target_app,
                source_app_id,
            )
            if not health["healthy"] or not health["checkpoint_match"]:
                raise RuntimeError(
                    "Post-migration health or checkpoint verification failed"
                )

            results.append({
                "source": source_app,
                "target": target_app,
                "status": "success",
                "message": "Deployed successfully",
            })
            completed += 1
            print_progress(completed, len(pending), source_app, "success")
            print()

        except Exception as e:
            results.append({
                "source": source_app,
                "target": target_app,
                "status": "failed",
                "message": str(e),
            })
            completed += 1
            print_progress(completed, len(pending), source_app, "failed")
            print(f"\n    Error: {e}")

    # Summary
    successes = sum(1 for r in results if r["status"] == "success")
    failures = sum(1 for r in results if r["status"] == "failed")
    dry_runs = sum(1 for r in results if r["status"] == "dry-run")
    blocked_count = sum(1 for r in results if r["status"] == "blocked")
    needs_input_count = sum(
        1 for r in results if r["status"] == "needs-input"
    )
    existing_target_count = sum(
        1 for r in results if r["status"] == "already-migrated"
    )

    print()
    print("=" * 70)
    print("Bulk Migration Summary")
    print("=" * 70)
    processed = (
        successes
        + failures
        + dry_runs
        + existing_target_count
    )
    pct = (processed / len(pending) * 100) if len(pending) > 0 else 100
    print(
        f"  Success: {successes} | Failed: {failures} | "
        f"Dry-run: {dry_runs} | Needs input: {needs_input_count} | "
        f"Blocked: {blocked_count} | Already migrated: {existing_target_count}"
    )
    print(f"  Processed: {pct:.1f}%")

    if failures > 0:
        print(f"\n  Failed migrations:")
        for r in results:
            if r["status"] == "failed":
                print(f"    ✗ {r['source']} → {r['target']}: {r['message']}")

    # Write results to JSON
    with open(results_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "results": results,
                "summary": {
                    "success": successes,
                    "failed": failures,
                    "dry_run": dry_runs,
                    "blocked": blocked_count,
                    "needs_input": needs_input_count,
                    "existing_target": existing_target_count,
                    "total": len(pending),
                },
            },
            f,
            indent=2,
        )
    print(f"\n  Results saved to {results_file}")

    return (
        0
        if (
            failures == 0
            and blocked_count == 0
            and needs_input_count == 0
        )
        else 1
    )


def main():
    parser = argparse.ArgumentParser(
        description="Run bulk migration from a CSV plan",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bulk_migrate.py --input-csv migration-plan.csv --dry-run
  python bulk_migrate.py --input-csv migration-plan.csv
  python bulk_migrate.py --input-csv migration-plan.csv --confirm
  python bulk_migrate.py --input-csv migration-plan.csv --dry-run \\
    --system-identity-action create
CSV columns (from inventory.py --export-csv):
  source_app_name, source_subscription_name, source_resource_group, source_subscription_id,
  source_location, source_created_at, source_last_modified_at,
  source_managed_resource_group, source_backing_container_app_name,
  source_linked_container_app_environment_name,
  source_linked_container_app_environment_resource_group,
  source_function_app_id, source_backing_container_app_id,
  source_linked_container_app_environment_id, migration_status, migration_blocker,
  target_resource_group, target_app_name, system_identity_action
""",
    )
    parser.add_argument(
        "--input-csv",
        required=True,
        help="Inventory-generated migration plan CSV (required)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read live sources and validate target requests without deploying (default: false)",
    )
    parser.add_argument(
        "--include-migrated",
        action="store_true",
        help="Also process rows already marked as Migrated (default: false)",
    )
    parser.add_argument(
        "--results-file",
        help="Result JSON override (default: <input-csv-stem>-results.json in the current directory)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Approve and execute a reviewed plan without a TTY prompt (default: false)",
    )
    parser.add_argument(
        "--allow-update-existing",
        action="store_true",
        help="Allow updates when destination Container Apps already exist (default: false)",
    )
    parser.add_argument(
        "--checkpoint-csv",
        help="Checkpoint CSV override (default: <input-csv-stem>-checkpoints.csv in the current directory)",
    )
    parser.add_argument(
        "--system-identity-action",
        choices=["create", "skip"],
        help="Default system identity decision for rows where the CSV column is blank (default: none; CSV row value takes precedence)",
    )
    args = parser.parse_args()

    print(f"\nLoading migration plan from {args.input_csv}...")
    rows = load_csv(args.input_csv)

    if not rows:
        print("✗ CSV is empty")
        sys.exit(1)

    errors = validate_csv(rows)
    if errors:
        print(f"\n✗ CSV validation failed ({len(errors)} errors):")
        for err in errors[:10]:
            print(f"  - {err}")
        if len(errors) > 10:
            print(f"  ... and {len(errors) - 10} more")
        sys.exit(1)

    print(f"✓ Loaded {len(rows)} rows")
    checkpoint_csv = (
        Path(args.checkpoint_csv).expanduser().resolve()
        if args.checkpoint_csv
        else checkpoint_csv_for_input(args.input_csv)
    )
    results_file = (
        Path(args.results_file).expanduser().resolve()
        if args.results_file
        else results_json_for_input(args.input_csv)
    )
    print(f"Checkpoint CSV: {checkpoint_csv}")
    print(f"Results JSON: {results_file}")
    exit_code = run_bulk_migration(
        rows,
        dry_run=args.dry_run,
        skip_migrated=not args.include_migrated,
        results_file=str(results_file),
        skip_confirmation=args.confirm,
        allow_update_existing=args.allow_update_existing,
        checkpoint_csv=str(checkpoint_csv),
        default_system_identity_action=args.system_identity_action,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.", file=sys.stderr)
        raise SystemExit(130)
