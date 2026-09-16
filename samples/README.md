# Sanitized examples

All names and identifiers below are placeholders.

## Inventory

```bash
python inventory.py --subscription-id 00000000-0000-0000-0000-000000000000
```

Example output: `inventory-output.txt`

## Export a bulk plan

```bash
python inventory.py \
  --subscription-id 00000000-0000-0000-0000-000000000000 \
  --export-csv migration-plan.csv
```

Example CSV: `migration-plan.csv`

## Interactive migration

```bash
python migrate_function_app.py \
  --interactive \
  --source-subscription-id 00000000-0000-0000-0000-000000000000
```

Example output: `interactive-migration-output.txt`

## Source ARM ID

```bash
python migrate_function_app.py \
  --source-app-id "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/example-rg/providers/Microsoft.Web/sites/orders-function" \
  --dry-run
```

## Check migration health

```bash
python migrate_function_app.py \
  --check-health \
  --checkpoint-csv migration-checkpoints.csv
```

Example output: `health-check-output.txt`
