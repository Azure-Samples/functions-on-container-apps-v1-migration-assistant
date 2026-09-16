# Functions on Container Apps v1 Migration Assistant

This repository migrates legacy Azure Functions-on-Container-Apps proxy apps to native Container Apps Function Apps.

## Source of truth

Use the customer scripts:

- `inventory.py`
- `migrate_function_app.py`
- `bulk_migrate.py`

Read `README.md` before execution. Do not implement an alternate migration path.

## Autonomous workflow

1. Prepare the environment with `setup.sh` or `setup.ps1`.
2. Run inventory for the requested subscription scope.
3. Present total, eligible, needs-input, and blocked counts.
4. Show all blocked apps and reasons.
5. Collect required identity decisions.
6. For one app, use interactive migration.
7. For multiple apps, export CSV and run bulk dry-run.
8. Stop before Azure writes and request confirmation.
9. Execute only confirmed ready apps.
10. Wait for and verify each tagged target's health before reporting success.

Use `migrate_function_app.py --check-health` for post-migration health checks.
Use `migrate_function_app.py --check-health --checkpoint-csv <FILE>` to check all recorded migrations.

## Invariants

- The source must be `Microsoft.Web/sites` with kind containing `azurecontainerapps`.
- A healthy backing Container App must exist.
- Copy the live backing ARM body; do not rebuild it property-by-property.
- Preserve the source subscription and Container Apps environment.
- Keep customer-editable bulk destination resource group and app name.
- Keep secret values in memory only.
- Never overwrite untagged targets silently.
- Never delete the source proxy automatically.
- Tag targets with `migrate-via-tool=true`.
- Tag targets with the source ARM ID in `migration-source-id`.

## Local validation

Unit tests are isolated under `local_tests/`.

```bash
python -m unittest discover -s local_tests -v
```
