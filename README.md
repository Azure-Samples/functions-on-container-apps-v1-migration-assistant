# Functions on Container Apps v1 Migration Assistant

Migrate legacy Azure Functions-on-Container-Apps proxy resources to native `Microsoft.App/containerApps` resources with `kind=functionapp`.

## How it works

The toolkit:

1. Finds `Microsoft.Web/sites` resources whose kind contains `azurecontainerapps`.
2. Resolves the platform-managed backing Container App through `managedBy`.
3. Reads the live backing ARM configuration with API version `2026-01-01`.
4. Copies that working configuration instead of reconstructing it.
5. Removes proxy/read-only fields, rehydrates app-setting secrets, sets `kind=functionapp`, and keeps the same Container Apps environment.

Containers, images, registries, identities, ingress, Dapr, probes, volumes, scaling, CPU, memory, and ephemeral storage are preserved.

## Setup

Bash:

```bash
./setup.sh
source .venv/bin/activate
az login
```

PowerShell:

```powershell
pwsh -File ./setup.ps1

# Windows
& .\.venv\Scripts\Activate.ps1

# Linux or macOS
& ./.venv/bin/Activate.ps1

az login
```

Python 3.10 or later is required.

## Inventory

Scan one subscription:

```bash
python inventory.py --subscription-id <SUBSCRIPTION_ID>
```

Scan all accessible subscriptions:

```bash
python inventory.py --all-subscriptions
```

The summary reports:

- Total v1 apps
- Eligible for migration
- Customer input required
- Migration blocked

Ready rows are green, input-required rows are yellow, and blocked rows are red in a TTY. Summary counts, JSON, CSV, and redirected output are never colored.

Optional output:

```bash
python inventory.py --all-subscriptions --json
python inventory.py --all-subscriptions --export-csv
python inventory.py --all-subscriptions --all-columns
python inventory.py --all-subscriptions \
  --columns subscription-id,subscription-name,resource-group,app-name,created-at
```

CSV files are created in the current directory and the absolute path is printed.

## Interactive migration

```bash
python migrate_function_app.py \
  --interactive \
  --source-subscription-id <SUBSCRIPTION_ID>
```

The workflow filters eligible apps, asks for the target app name, shows complete source and destination ARM IDs, and requires confirmation before deployment.
After deployment, it waits for the destination to become healthy, verifies the migration checkpoint tags, prints the health report, and returns failure if verification does not pass.

Use a source ARM ID for non-interactive preparation or migration:

```bash
python migrate_function_app.py \
  --source-app-id "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Web/sites/<FUNCTION_APP>" \
  --dry-run
```

Single-app migration keeps the source subscription, resource group, and Container Apps environment. A system-assigned identity requires an explicit `create` or `skip` decision.

Check the current health of an existing target:

```bash
python migrate_function_app.py \
  --source-app-id "/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Web/sites/<FUNCTION_APP>" \
  --target-app <TARGET_APP> \
  --check-health
```

This reports resource health and migration-checkpoint status without making changes.

Check all successfully recorded migrations:

```bash
python migrate_function_app.py \
  --check-health \
  --checkpoint-csv migration-checkpoints.csv
```

## Bulk migration

```bash
python inventory.py \
  --subscription-id <SUBSCRIPTION_ID> \
  --export-csv

python bulk_migrate.py \
  --input-csv <MIGRATION_PLAN.csv> \
  --dry-run

python bulk_migrate.py \
  --input-csv <MIGRATION_PLAN.csv> \
  --confirm
```

The CSV exposes only useful customer choices:

- `target_resource_group`
- `target_app_name`
- `system_identity_action` when required

Set a default identity decision for blank CSV rows:

```bash
python bulk_migrate.py \
  --input-csv <MIGRATION_PLAN.csv> \
  --dry-run \
  --system-identity-action create
```

An explicit CSV row value takes precedence over the command-line default.

Blocked apps are skipped with a reason. Apps requiring input are skipped until the CSV is completed.
Interactive inventory shows migration status and destination app, and lists tagged completed migrations in a separate source-to-destination table.
Bulk migration performs the same health and checkpoint verification after each deployed app; an unhealthy target is recorded as a failed migration.

## Migration checkpoint

The tagged destination Container App is authoritative. After each successful single deployment, the tool appends to `migration-checkpoints.csv`. Bulk migration derives separate filenames from the input plan and creates them in the current working directory:

```text
migration-plan.csv -> migration-plan-checkpoints.csv
migration-plan.csv -> migration-plan-results.json
```

The checkpoint CSV provides health-check input. The result JSON records per-row outcomes and summary counts.

Destination resources receive:

```text
migrate-via-tool=true
migration-source-id=<SOURCE_FUNCTION_APP_ARM_ID>
```

The checkpoint CSV records source and target ARM IDs, names, resource groups, environment ID, and migration time. Override derived paths with `--checkpoint-csv` or `--results-file`.

## Delete a verified v1 source manually

**Warning: This is a destructive and irreversible operation.** Deleting the legacy `Microsoft.Web/sites` Function App can also remove its platform-managed backing resources. The toolkit intentionally does not automate source deletion.

Delete the source only after all of these are true:

1. `--check-health` reports the target as healthy and the checkpoint as matched.
2. Every trigger and binding has been exercised successfully.
3. Storage, identities, RBAC, networking, secrets, and telemetry are verified.
4. Production traffic, DNS, callers, and schedules have moved to the v2 app.
5. The v2 app has completed the required observation period.
6. The source ARM ID has been reviewed by another operator.

Manual deletion:

```bash
SOURCE_APP_ID="/subscriptions/<SUBSCRIPTION_ID>/resourceGroups/<RESOURCE_GROUP>/providers/Microsoft.Web/sites/<FUNCTION_APP>"

az resource delete \
  --ids "$SOURCE_APP_ID" \
  --api-version 2026-03-15
```

Verify that the deleted source no longer appears in inventory:

```bash
python inventory.py --subscription-id <SUBSCRIPTION_ID>
```

## Samples

Sanitized commands and output are under `samples/`.

## Limitations

- System-assigned identities receive a new principal; validate RBAC.
- Validate triggers, bindings, storage, networking, telemetry, and health before traffic cutover.
- Delete the legacy proxy only after confirming the native v2 app is serving production traffic.
