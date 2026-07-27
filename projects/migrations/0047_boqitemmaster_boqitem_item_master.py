from django.db import migrations, models
import django.db.models.deletion


# Verbatim snapshot of the literal list that get_standard_boq_items() returned before
# this migration. The function now reads BOQItemMaster instead; this copy exists solely
# to seed the catalogue and must never be edited — later catalogue changes belong in the
# admin screen, not here.
STANDARD_BOQ_ITEMS = [
        {'serial_no':  1, 'category': 'Solar Modules', 'description': '595Wp Solar modules DCR',                                                                           'uom': 'Nos'},
        {'serial_no':  2, 'category': 'Solar Modules', 'description': 'Module Transport',                                                                                  'uom': 'Nos'},
        {'serial_no':  3, 'category': 'Structure',     'description': 'Module Mounting Structure with STAAD report HDGI/GI',                                               'uom': 'LOT'},
        {'serial_no':  4, 'category': 'Structure',     'description': 'Module Mounting Structures transport',                                                              'uom': 'LOT'},
        {'serial_no':  5, 'category': 'Inverter',      'description': '10 kW Grid-Tie Inverter Single Phase',                                                              'uom': 'Nos'},
        {'serial_no':  6, 'category': 'Inverter',      'description': 'Data Logger if required',                                                                           'uom': 'Nos'},
        {'serial_no':  7, 'category': 'Inverter',      'description': 'Inverter Transport',                                                                                'uom': 'Nos'},
        {'serial_no':  8, 'category': 'BOS',           'description': '1CX4sqmm XLPE Tin Cu string cables RED',                                                           'uom': 'Mtr'},
        {'serial_no':  9, 'category': 'BOS',           'description': '1CX4sqmm XLPE Tin Cu string cables Black',                                                         'uom': 'Mtr'},
        {'serial_no': 10, 'category': 'BOS',           'description': '4C X 6mm2 XLPE Tin Cu cables',                                                                     'uom': 'Mtr'},
        {'serial_no': 11, 'category': 'BOS',           'description': '1CX6mm2 Earthing cable Green',                                                                     'uom': 'Mtr'},
        {'serial_no': 12, 'category': 'BOS',           'description': 'MC4 Connectors Male and Female',                                                                   'uom': 'Nos'},
        {'serial_no': 13, 'category': 'BOS',           'description': 'PVC Conduit 25MM',                                                                                 'uom': 'Mtr'},
        {'serial_no': 14, 'category': 'BOS',           'description': 'Flexible Conduit 25MM GI/PVC',                                                                     'uom': 'Mtr'},
        {'serial_no': 15, 'category': 'BOS',           'description': 'PVC Elbow 25MM',                                                                                   'uom': 'Nos'},
        {'serial_no': 16, 'category': 'BOS',           'description': 'PVC Tee 25MM',                                                                                     'uom': 'Nos'},
        {'serial_no': 17, 'category': 'BOS',           'description': 'PVC Nail Clip 25MM 150PCS',                                                                        'uom': 'Pkt'},
        {'serial_no': 18, 'category': 'BOS',           'description': 'CU Pin LUG 4 SQMM',                                                                               'uom': 'Nos'},
        {'serial_no': 19, 'category': 'BOS',           'description': 'CU Ring LUG 4 SQMM',                                                                              'uom': 'Nos'},
        {'serial_no': 20, 'category': 'BOS',           'description': 'CU Pin LUG 6 SQMM',                                                                               'uom': 'Nos'},
        {'serial_no': 21, 'category': 'BOS',           'description': 'CU Ring LUG 6 SQMM',                                                                              'uom': 'Nos'},
        {'serial_no': 22, 'category': 'BOS',           'description': 'Cable Tie 300MM UV resistant',                                                                     'uom': 'Pkt'},
        {'serial_no': 23, 'category': 'BOS',           'description': 'PVC Tape Red Blue Black Yellow Green',                                                             'uom': 'Nos'},
        {'serial_no': 24, 'category': 'BOS',           'description': 'Silver Spray Paint',                                                                               'uom': 'Nos'},
        {'serial_no': 25, 'category': 'BOS',           'description': 'Lockfix for fixing fastener in RCC roof 500ML',                                                    'uom': 'Nos'},
        {'serial_no': 26, 'category': 'BOS',           'description': 'HSV Hilti M12 Mechanical Wedge Anchors L-100MM',                                                   'uom': 'Nos'},
        {'serial_no': 27, 'category': 'BOS',           'description': 'Fasteners Inverter Mounting',                                                                      'uom': 'Nos'},
        {'serial_no': 28, 'category': 'BOS',           'description': 'ACDB-10KW 3P MCB4P 16 AMPS',                                                                      'uom': 'Nos'},
        {'serial_no': 29, 'category': 'BOS',           'description': 'Copper Bonded Earthing Rod 1MTR chemical earthing',                                                'uom': 'Nos'},
        {'serial_no': 30, 'category': 'BOS',           'description': 'Earthing Compound bag 25KG ECOSOLX',                                                               'uom': 'Nos'},
        {'serial_no': 31, 'category': 'BOS',           'description': 'Heavy duty synthetic circular chamber',                                                            'uom': 'Nos'},
        {'serial_no': 32, 'category': 'BOS',           'description': 'Conventional Lightning Arrestor 1Mtr IEC62305',                                                    'uom': 'Nos'},
        {'serial_no': 33, 'category': 'BOS',           'description': 'PU Foam Sealant Spray 750ml for joint filling',                                                    'uom': 'Nos'},
        {'serial_no': 34, 'category': 'BOS',           'description': 'Module Cleaning System without motor',                                                             'uom': 'Nos'},
        {'serial_no': 35, 'category': 'BOS',           'description': 'Site Installation charges including civil work',                                                    'uom': 'Nos'},
        {'serial_no': 36, 'category': 'BOS',           'description': 'Miscellaneous - (net metering,transportation,rubber mat,fire extinguishers,warning boards)',             'uom': 'Nos'},
        {'serial_no': 37, 'category': 'BOS',           'description': 'Contingency',                                                                                      'uom': 'LS'},
]

DEFAULT_UNIT = 'Nos'          # Applied only to seed rows whose source dict carries no uom
CODE_TEMPLATE = 'ITM-{:03d}'  # Deterministic: ITM-001 … ITM-037, by position in the list above


def seed_catalogue(apps, schema_editor):
    """(a) Create the catalogue rows from the pre-migration literal. code and sort_order
    both follow the literal's serial_no, so the template order is preserved exactly."""
    BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')

    rows, defaulted = [], []
    for item in STANDARD_BOQ_ITEMS:
        unit = (item.get('uom') or '').strip()
        if not unit:
            unit = DEFAULT_UNIT
            defaulted.append(item['description'])
        rows.append(BOQItemMaster(
            code=CODE_TEMPLATE.format(item['serial_no']),
            description=item['description'],
            unit=unit,
            category=item['category'],
            is_active=True,
            sort_order=item['serial_no'],
        ))
    BOQItemMaster.objects.bulk_create(rows)

    print("")
    print(f"[0047] BOQItemMaster seeded: {len(rows)} rows "
          f"({CODE_TEMPLATE.format(1)}..{CODE_TEMPLATE.format(len(rows))})")
    if defaulted:
        print(f"[0047] {len(defaulted)} item(s) had no uom in the source list and were "
              f"defaulted to '{DEFAULT_UNIT}':")
        for description in defaulted:
            print(f"[0047]   - {description}")
    else:
        print("[0047] All seeded items carried a uom in the source list; none defaulted.")


def unseed_catalogue(apps, schema_editor):
    """Reverse of (a): delete only the rows this migration created. Touches no BOQItem —
    their FKs were already nulled by the reverse of (b), which runs first."""
    BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')
    codes = [CODE_TEMPLATE.format(item['serial_no']) for item in STANDARD_BOQ_ITEMS]
    deleted, _ = BOQItemMaster.objects.filter(code__in=codes).delete()
    print("")
    print(f"[0047 reverse] Deleted {deleted} seeded BOQItemMaster row(s).")


def backlink_boq_items(apps, schema_editor):
    """(b) Point existing BOQItem rows at their catalogue entry. Exact description match
    only — no normalisation, no case folding. Unmatched rows keep item_master null."""
    BOQItem       = apps.get_model('projects', 'BOQItem')
    BOQItemMaster = apps.get_model('projects', 'BOQItemMaster')

    for master in BOQItemMaster.objects.all():
        BOQItem.objects.filter(description=master.description).update(item_master=master)


def unbacklink_boq_items(apps, schema_editor):
    """Reverse of (b): null the FK. Quantities, descriptions and row counts are untouched."""
    BOQItem = apps.get_model('projects', 'BOQItem')
    cleared = BOQItem.objects.filter(item_master__isnull=False).update(item_master=None)
    print("")
    print(f"[0047 reverse] Cleared item_master on {cleared} BOQItem row(s).")


def report_backlink(apps, schema_editor):
    """(c) Print the link outcome so a partial match is visible in the migrate output
    rather than discovered later during aggregation."""
    BOQItem = apps.get_model('projects', 'BOQItem')

    total    = BOQItem.objects.count()
    linked   = BOQItem.objects.filter(item_master__isnull=False).count()
    unlinked = total - linked

    print("")
    print(f"[0047] BOQItem rows: {total} total, {linked} linked, {unlinked} left null")
    if unlinked:
        unmatched = sorted(set(
            BOQItem.objects.filter(item_master__isnull=True)
                           .values_list('description', flat=True)
        ))
        print(f"[0047] {len(unmatched)} distinct unmatched description(s) "
              f"(showing up to 50):")
        for description in unmatched[:50]:
            print(f"[0047]   - {description!r}")


def report_noop(apps, schema_editor):
    """(c) has nothing to undo — reporting wrote no data."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0046_alter_project_customer_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='BOQItemMaster',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('code', models.CharField(max_length=32, unique=True)),
                ('description', models.CharField(max_length=255)),
                ('unit', models.CharField(max_length=20)),
                ('category', models.CharField(blank=True, max_length=64)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'ordering': ['sort_order', 'code'],
            },
        ),
        migrations.AddField(
            model_name='boqitem',
            name='item_master',
            field=models.ForeignKey(blank=True, null=True,
                                    on_delete=django.db.models.deletion.SET_NULL,
                                    related_name='boq_items', to='projects.boqitemmaster'),
        ),
        # (a) seed the catalogue
        migrations.RunPython(seed_catalogue, unseed_catalogue),
        # (b) back-link existing BOQ rows by exact description
        migrations.RunPython(backlink_boq_items, unbacklink_boq_items),
        # (c) report the outcome
        migrations.RunPython(report_backlink, report_noop),
    ]
