# Migration rehearsal — run the pending migrations against a copy of production

Horizon Solar PMS · written by session B1, 25 Sep 2026

**Why this file exists.** Before 25 Sep 2026 the repo had no written procedure for this.
`DEPLOY_PLAN.md` §1 says to take a Railway backup and "confirm you know how to restore it";
§2 lists unapplied migrations against a read-only URL. Neither one RUNS a migration against
production's data. `tests_migration_chain` proves the chain applies to an EMPTY database,
which is a different question: a migration that refuses on rows it finds, or a constraint
that existing rows violate, passes that test and still stops the deploy.

Railway's start command is `migrate --run-syncdb && collectstatic && gunicorn`. A migration
that exits non-zero means gunicorn never starts, which is what happened on 3 Sep 2026
(`execution-model.md` R-22). The rehearsal finds that on a copy first.

**Nothing here writes to production.** `pg_dump` only reads. Every migrate runs against a
local scratch database that is dropped at the end.

---

## 1. Take a fresh dump

The `railway_backup.dump` in the repo root is from 28 Aug 2026. It is too old to rehearse
against.

Railway → Postgres → **Connect** → the **public** connection URL. Check the server version
first, because `pg_dump` must be the same major version or newer. The local tools are
PostgreSQL 18.

```powershell
psql "<Railway public DATABASE_URL>" -c "SHOW server_version;"
```

Write the dump **outside the repository**. `.gitignore` ignores exactly one name,
`railway_backup.dump`, so any other name in the repo root is one `git add -A` away from being
committed.

```powershell
New-Item -ItemType Directory -Force "$HOME\horizon-pms-dumps" | Out-Null
pg_dump --format=custom --no-owner --no-acl -d "<Railway public DATABASE_URL>" -f "$HOME\horizon-pms-dumps\railway_2026-09-25.dump"
```

## 2. Restore it into a scratch database

```powershell
psql -h localhost -U solarpms_user -d postgres -c "CREATE DATABASE solarpms_rehearsal;"
pg_restore --no-owner --no-acl -h localhost -U solarpms_user -d solarpms_rehearsal "$HOME\horizon-pms-dumps\railway_2026-09-25.dump"
```

`psql` and `pg_restore` prompt for the password. Do not put it on the command line or in
shell history.

## 3. Point Django at it and run what Railway will run

`python-decouple` reads the environment before `.env`, so this override wins over the
local database for this shell only.

```powershell
$env:DATABASE_URL="postgresql://solarpms_user:<password>@localhost:5432/solarpms_rehearsal"
python manage.py showmigrations projects | Select-String "\[ \]"   # what is pending
python manage.py migrate --run-syncdb --noinput                     # Railway's exact command
python manage.py migrate projects <the migration before the newest> # prove the reverse
python manage.py migrate projects                                   # forward again
python manage.py makemigrations --check --dry-run                   # "No changes detected"
Remove-Item Env:\DATABASE_URL
```

For B1 the reverse target is `0101`, and the newest migration is `0102`.

**Read the migrate output, not only the exit code.** Several migrations print what they did.
0102 prints `B1: N change request(s) set to origin=scm; M left at origin=pm.`

## 4. Drop the scratch database

```powershell
psql -h localhost -U solarpms_user -d postgres -c "DROP DATABASE solarpms_rehearsal;"
```

Keep the dump file outside the repo, or delete it. It is production data.

---

## Migrations that can refuse on real data (as of 0102)

These are read from the migration files themselves. Each one stops the whole deploy if it
fails, and every migration after it is then never reached. If production is behind, the
rehearsal runs all of them in order.

| Migration | Refuses or fails when | What it does about it |
|---|---|---|
| `0093_payment_request_vendor_order_not_null` | any `PaymentRequest` has no `vendor_order`. **Every payment raised before O1 has none**, because `0091` adds the column as nullable and nothing backfills it. | `RuntimeError('0093 refused: … have no vendor_order (pk …)')`. Nothing is changed. An operator attaches each one to a VendorOrder or removes it deliberately. |
| `0094_uniq_invoice_number_per_order` | two invoice documents on one order share an `invoice_number` | a plain `AddConstraint`, which fails with an IntegrityError |
| `0097_vendor_order_total_amount` | any `VendorOrder` has no priced line (`Sum(lines.amount)` null or ≤ 0) | `RuntimeError('0097 refused: …')` |
| `0099_payment_request_approved_amount` | an `approved` / `confirmed` payment has `amount` ≤ 0 (the backfill copies `amount` into `approved_amount`, which must be > 0) | IntegrityError on the backfill UPDATE |
| `0101_payment_request_drop_legacy_fields` | any `PaymentRequest` holds a value in `boq_item`, `invoice_number` or an `invoice_document_*` column (any text but `''`, or a non-NULL `boq_item`) | `RuntimeError('0101 refused: … hold a value in a legacy column …')`. Nothing is changed. Move each value onto a VendorOrderDocument or clear it deliberately. |

`0092` rewrites every `PaymentRequest.status` of `pending` to `approved`. It changes data and
cannot refuse. `0102` backfills `DesignChangeRequest.origin` and cannot refuse: every pre-B1
row satisfies all of its constraints.

### Read-only checks to run in the Railway shell first

First see where production is. **Run these read-only checks only for migrations still listed
as pending.**

```python
python manage.py showmigrations projects
```

Then, in `python manage.py shell`:

```python
from django.db import connection
with connection.cursor() as c:
    c.execute("SELECT id FROM projects_paymentrequest ORDER BY id")
    print('PaymentRequest pks:', [r[0] for r in c.fetchall()])
```

- **Before 0091** (no `vendor_order_id` column): every pk printed will make **0093 refuse**.
- **At 0091 or 0092:** filter it further:
  `SELECT id FROM projects_paymentrequest WHERE vendor_order_id IS NULL`.
- **Before 0101:** run 0101's own check. It is plain SQL, written so it can be run like this:

```python
from importlib import import_module
from django.db import connection
m = import_module('projects.migrations.0101_payment_request_drop_legacy_fields')
print(m.legacy_value_pks(connection, 'projects_paymentrequest'))
```

An empty list means 0101 will not refuse.
