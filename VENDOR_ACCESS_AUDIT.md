# Vendor management access audit

**Investigate-only session. No application code touched.** Audited 2026-09-06 against
`main` at `462f618`, working tree clean of tracked changes.

---

## The answer

**SCM can already manage vendors today — add, edit and deactivate — and no change is
required to grant it.** All four views gate on an inline `if profile.role not in ('SCM',
'Admin')` check, SCM is inside that tuple, and the SCM dashboard renders a "Vendors" button
that reaches `vendor_list`. Vendors are entirely global — no field on `Vendor`,
`VendorCategory` or `VendorBrand` scopes to a project, program, project type or role — so
"applicable to both Residential and OPEX" is already true by construction and needs no work.
Deactivation is real, not decorative: both places a vendor is chosen (Delivery Challan create
form, SCM payment-request modal) filter `is_active=True` on the dropdown **and** re-apply
that filter on the POST resolve. The premise of the request — that these views need to be
opened up to SCM — is false. What is actually missing is different and smaller, and is listed
under "What is actually broken" below.

The one role that arguably *should* reach these screens and cannot is **System Admin**;
`'System Admin'` is a distinct `UserProfile.ROLE_CHOICES` value from `'Admin'` and is not in
the tuple. And **`Admin` has no rendered control anywhere** — for an Admin, all four views are
raw-URL-only.

---

## V1 — Access, exactly

All four live in `projects/views.py` under the `# Vendor Master` banner. Verbatim decorator
stacks and first docstring lines:

### `vendor_list`

```python
@login_required
def vendor_list(request):
    """
    List all vendors with optional category and active/inactive filters.
    Access: SCM and Admin only (inline role check — not via decorator).
    """
    profile = request.user.profile
    # Inline role check used here (not @role_required) because vendor views
    # are shared between SCM and Admin with identical permission logic
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()
```

### `vendor_add`

```python
@login_required
def vendor_add(request):
    """
    Add a new vendor. Warns (but does not block) if a vendor with the same name
    already exists — duplicate names are allowed to handle trading variants.
    VendorBrand entries (make_brand + optional category) are saved from the
    dynamic brand rows submitted alongside the main form.
    Access: SCM and Admin only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()
```

### `vendor_edit`

```python
@login_required
def vendor_edit(request, vendor_id):
    """
    Edit an existing vendor's details. Replaces all VendorBrand entries with
    the brand rows submitted in the form.
    Access: SCM and Admin only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()
```

### `vendor_toggle_status`

```python
@login_required
def vendor_toggle_status(request, vendor_id):
    """
    Toggle a vendor's active/inactive status. Returns JSON so the UI can update
    the toggle button without a full page reload.
    Access: SCM and Admin only. POST only.
    """
    profile = request.user.profile
    if profile.role not in ('SCM', 'Admin'):
        return HttpResponseForbidden()
    if request.method != 'POST':
        return HttpResponseForbidden()

    vendor = get_object_or_404(Vendor, pk=vendor_id)
    vendor.is_active = not vendor.is_active
    vendor.save()
    return JsonResponse({'is_active': vendor.is_active})
```

**Roles admitted:** exactly `'SCM'` and `'Admin'`, on all four. The only decorator is
`@login_required`; no `@role_required` is applied to any of them.

**Docstring vs decorator — do they match?** Yes, on all four. This is one of the few places in
this codebase where the claim and the gate agree, and `vendor_list`'s docstring even flags
that the check is inline rather than decorator-based. `vendor_toggle_status`'s "POST only"
claim is also honoured.

**But the docstring's word "Admin" is narrower than a reader will assume.**
`UserProfile.ROLE_CHOICES` is:

```python
    ROLE_CHOICES = [
        ('Admin',               'Admin'),
        ('System Admin',        'System Admin'),
        ('PM',                  'PM'),
        ('Project Coordinator', 'Project Coordinator'),
        ('Site Engineer',       'Site Engineer'),
        ('Design',        'Design'),
        ...
        ('Finance',       'Finance'),
        ('SCM',           'SCM'),
        ('CEO',           'CEO'),
        ('BD',            'BD'),
    ]
```

`'System Admin'` is a separate value and is **not** admitted. Neither is `'CEO'`. A System
Admin gets a bare 403 on all four.

---

## V2 — Reachability

Every template reference to the four URL names, repo-wide:

```
./projects/templates/dashboard/scm.html:27:  <a href="{% url 'vendor_list' %}" class="btn btn-outline-secondary btn-sm">Vendors</a>
./projects/templates/vendors/vendor_form.html:205:            <a href="{% url 'vendor_list' %}" class="btn btn-outline-secondary">Cancel</a>
./projects/templates/vendors/vendor_list.html:7:  <a href="{% url 'vendor_add' %}" class="btn btn-primary btn-sm">+ Add Vendor</a>
./projects/templates/vendors/vendor_list.html:33:    <a href="{% url 'vendor_list' %}" class="btn btn-link btn-sm">Clear</a>
./projects/templates/vendors/vendor_edit ... (see below)
```

### The single entry point — `dashboard/scm.html`

```django
<div class="d-flex justify-content-between align-items-center mb-3">
  <h5 class="mb-0 fw-semibold">SCM Dashboard</h5>
  {# Quick link to vendor master — SCM manages vendor records from here #}
  <a href="{% url 'vendor_list' %}" class="btn btn-outline-secondary btn-sm">Vendors</a>
</div>
```

**There is no template condition around it.** The gate is the view that renders this template:

```python
@login_required
@role_required(['SCM'])
def dashboard_scm(request):
```

So the only rendered route into vendor management is the SCM dashboard, and that dashboard is
SCM-only. `vendor_add`, `vendor_edit` and `vendor_toggle_status` are reachable only from
inside `vendor_list`.

### `vendors/vendor_list.html` — the controls, ungated

```django
<div class="d-flex justify-content-between align-items-center mb-4">
  <h4 class="mb-0">Vendor Master</h4>
  <a href="{% url 'vendor_add' %}" class="btn btn-primary btn-sm">+ Add Vendor</a>
</div>
```

```django
          <td>
            <a href="{% url 'vendor_edit' vendor.pk %}"
               class="btn btn-outline-secondary btn-sm py-0 me-1">Edit</a>
            <button type="button"
                    class="btn btn-sm py-0 {% if vendor.is_active %}btn-outline-danger{% else %}btn-outline-success{% endif %}"
                    data-vendor-id="{{ vendor.pk }}"
                    data-vendor-name="{{ vendor.name }}"
                    data-is-active="{{ vendor.is_active|yesno:'true,false' }}"
                    onclick="toggleVendor(this)">
              {% if vendor.is_active %}Deactivate{% else %}Activate{% endif %}
            </button>
          </td>
```

The only condition present is cosmetic (`{% if vendor.is_active %}` picks the button colour
and label). No role condition — correctly so, since reaching the page already required the
role.

### Navigation

**There is no navbar entry.** The only hit for "vendor" in `base.html` is unrelated:

```
projects/templates/base.html:244:  placeholder="Optional — add context, vendor name, ref number…"
```

No partial contains one either.

### Raw-URL-only?

**Yes, for `Admin`.** No Admin Panel screen, navbar item or dashboard renders any link to
`vendor_list`. An `Admin` user is admitted by all four views but has no rendered control that
would take them there — they must type `/projects/vendors/`. For SCM the screens are fully
reachable.

URL wiring, verbatim:

```python
    # Vendors — SCM and Admin only
    path('vendors/',                              views.vendor_list,          name='vendor_list'),
    path('vendors/add/',                          views.vendor_add,           name='vendor_add'),
    path('vendors/<int:vendor_id>/edit/',         views.vendor_edit,          name='vendor_edit'),
    path('vendors/<int:vendor_id>/toggle-status/', views.vendor_toggle_status, name='vendor_toggle_status'),  # Returns JSON {is_active: bool}
```

---

## V3 — Scoping

Full field lists, verbatim from `projects/models.py`:

```python
class VendorCategory(models.Model):
    """Lookup table for vendor categories (e.g. Solar Modules, Inverter, Structure)."""

    name = models.CharField(max_length=50, unique=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'Vendor Categories'

    def __str__(self):
        return self.name


class Vendor(models.Model):
    """Supplier / vendor in the master list. Used as make preferences in BOQ items."""

    name           = models.CharField(max_length=200)
    contact_person = models.CharField(max_length=100)
    phone          = models.CharField(max_length=15)
    email          = models.EmailField(null=True, blank=True)
    gst_number     = models.CharField(max_length=15, null=True, blank=True)
    msme_status    = models.BooleanField(default=False)
    msme_number    = models.CharField(max_length=50, null=True, blank=True)
    address        = models.TextField(null=True, blank=True)
    categories     = models.ManyToManyField(VendorCategory, related_name='vendors')
    is_active      = models.BooleanField(default=True)  # Inactive vendors hidden from BOQ dropdowns but kept for history
    created_by     = models.ForeignKey(
        'UserProfile',
        on_delete=models.SET_NULL,
        null=True,
    )
    created_at     = models.DateTimeField(auto_now_add=True)
    updated_at     = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_active', 'name']  # Active vendors surface first in lists

    def __str__(self):
        return self.name


class VendorBrand(models.Model):
    """
    A brand/make label supplied by a vendor, optionally scoped to one supply category.

    A vendor can have multiple brands for different categories — e.g. a single vendor
    may supply "Waaree" solar modules and "Polycab" BOS cables. These appear as
    separate entries in the BOQ Make/Preference dropdown, filtered to the item's category.

    If category is null the brand appears in all categories the vendor supplies.
    Vendors with no VendorBrand entries fall back to displaying the company name.
    """

    vendor     = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='brands')
    make_brand = models.CharField(max_length=200)
    # Optional — scope this brand label to one supply category.
    # Null means the brand shows across every category this vendor is assigned to.
    category   = models.ForeignKey(
        VendorCategory,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='vendor_brands',
    )

    class Meta:
        ordering = ['make_brand']

    def __str__(self):
        return f"{self.make_brand} — {self.vendor.name}"
```

### Relations

- `Vendor.categories` → M2M to `VendorCategory`, `related_name='vendors'`. Declares *what a
  vendor supplies*, not where.
- `VendorBrand.vendor` → FK to `Vendor`, `on_delete=CASCADE`, `related_name='brands'`.
- `VendorBrand.category` → nullable FK to `VendorCategory`, `on_delete=SET_NULL`,
  `related_name='vendor_brands'`.
- `Vendor.created_by` → nullable FK to `UserProfile`, `on_delete=SET_NULL`. Provenance only;
  nothing reads it for authorisation, filtering or display.

### Verdict

**Vendors are global.** There is no `project`, `program`, `project_type`, `role`, `region`,
`state` or `city` field on any of the three models, and no through-model carrying one. The
only axis of segmentation in the whole vendor subsystem is `VendorCategory` — a supply
category (Solar Modules, Inverter, Structure, BOS, Services, Other), which is a
material-type dimension, not a project dimension.

**This closes the "applicable to both Residential and OPEX" question with zero work.** Every
vendor is already visible to every project of either type. `Vendor.created_by` is the only
role-adjacent field and it is inert. Stop condition 3 is **not** triggered.

---

## V4 — Consumers

### Every FK / reference to `Vendor` in the schema

| Model | Field | `on_delete` | Nullable | `related_name` |
|---|---|---|---|---|
| `VendorBrand` | `vendor` | **`CASCADE`** | no | `brands` |
| `BOQItem` | `make_preference` | `SET_NULL` | yes | `preferred_items` |
| `BOQItem` | `ordered_vendor` | `SET_NULL` | yes | `ordered_items` |
| `DeliveryChallan` | `vendor` | `SET_NULL` | yes | `delivery_challans` |
| `PaymentRequest` | `vendor` | `SET_NULL` | yes | `payment_requests` |

Verbatim:

```python
    make_preference  = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='preferred_items',  # Vendor preferred by Design for this item
    )
```
```python
    ordered_vendor   = models.ForeignKey(
        Vendor,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ordered_items',  # Vendor actually selected by SCM when placing PO
    )
```
```python
    vendor = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL,
        null=True, blank=True, related_name='delivery_challans'
    )
```
```python
    vendor   = models.ForeignKey(
        Vendor, on_delete=models.SET_NULL, null=True,
        related_name='payment_requests',
    )
```
```python
    vendor     = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='brands')
```

Note the asymmetry: `VendorBrand` is the **only** `CASCADE`, and correctly so — a brand label
is owned by the vendor, whereas every historical record holds the vendor at arm's length via
`SET_NULL`. There is **no `PROTECT` anywhere**, which matters for V5.

### Views that read `Vendor`

All in `projects/views.py`:

| Site | What it does |
|---|---|
| `vendor_list`, `vendor_add`, `vendor_edit`, `vendor_toggle_status` | the four audited views |
| `_save_vendor_brands(vendor, post_data)` | helper called by `vendor_add` + `vendor_edit`; `vendor.brands.all().delete()` then re-creates |
| `dashboard_scm` — `scm_vendors` | vendor dropdown JSON for the raise-payment-request modal |
| `_build_vendors_by_category()` | builds the BOQ Make/Preference dropdown map; called once, from `boq_detail` |
| `boq_detail` | passes `vendors_by_category` into the template |
| `_boq_snapshot(boq)` | snapshots `ordered_vendor__name` + resolved `make_brand_label` into JSON |
| `scm_raise_payment_request` (POST handler) | `get_object_or_404(Vendor, pk=vendor_id, is_active=True)` |
| payment-confirm notification block | `pr.vendor.name` interpolated into the WhatsApp/email message body |
| `project_overview` — `dc_vendors` | vendor dropdown, SCM only |
| `create_delivery_challan` | `vendors` for the form; `Vendor.objects.get(pk=..., is_active=True)` on POST; `vendor_name` for `log_activity` |

### Forms

`projects/forms.py` — `VendorForm` only:

```python
class VendorForm(forms.ModelForm):

    class Meta:
        model  = Vendor
        fields = [
            'name', 'contact_person', 'phone', 'email', 'address',
            'gst_number', 'msme_status', 'msme_number', 'categories',
        ]
```

Note `is_active` and `created_by` are **not** form fields — status changes go only through
`vendor_toggle_status`, and `created_by` is stamped in `vendor_add`. That is the right shape.

### Templates that read `Vendor`

| Template | Read |
|---|---|
| `vendors/vendor_list.html` | full list + toggle controls |
| `vendors/vendor_form.html` | add/edit form + dynamic brand rows |
| `dashboard/scm.html` | `Vendors` link; `scm_vendors` JSON → `#scmPaymentVendorSelect` |
| `dashboard/finance.html` | `{{ pr.vendor }}` on pending payment requests |
| `dashboard/ceo.html` | `fin_vendor_payments_outstanding` (an aggregate — no vendor row read) |
| `projects/boq_detail.html` | `select.vendor-select` × 3, populated from `vendors_by_category`; `{{ item.ordered_vendor.name }}` |
| `projects/boq_history.html` | `{{ item.ordered_vendor__name }}` — from the **snapshot JSON**, not the FK |
| `projects/delivery_challan_create.html` | `{% for v in vendors %}` |
| `projects/delivery_challan_detail.html` | `{{ challan.vendor.name }}` |
| `projects/my_documents.html` | `{{ pr.vendor.name\|default:"—" }}` |
| `projects/payment_request_detail.html` | `{{ pr.vendor.name\|default:"—" }}` |
| `projects/project_overview.html` | `{{ dc.vendor.name }}`, `{{ pr.vendor.name }}`, `data-vendor="{{ pr.vendor.name }}"` in the confirm-payment modal |

### Management commands

Exactly one:

```
projects/management/commands/seed_opex_test_data.py:668:
        vendor  = Vendor.objects.filter(is_active=True).order_by('pk').first()
projects/management/commands/seed_opex_test_data.py:689:
            f'{vendor.name if vendor else "Unknown Vendor"}',
```

Read-only, and it respects `is_active`.

### Reports / exports

**None.** No CSV, XLSX or PDF export path touches `Vendor`.

---

## V5 — Deletion and mutation

### Is there any hard-delete path for a vendor?

**No — none, on any surface.**

- **View:** no `vendor_delete` / `delete_vendor` exists anywhere in the repo:
  ```
  $ grep -rni "vendor_delete\|delete_vendor" . --include=*.py --include=*.html
  (no matches)
  ```
- **URL:** `projects/urls.py` wires exactly four vendor paths; none is a delete.
- **Django admin:** `projects/admin.py` does **not** import or register `Vendor`,
  `VendorCategory` or `VendorBrand`. Its import block is:
  ```python
  from .models import (
      Project, Milestone, ProjectDocument, ProjectPhase, Task, UserProfile,
      NotificationLog, SystemSettings,
      Checklist, ChecklistItem, ChecklistTaskLink, ChecklistItemCompletion,
      Program,
      DesignAssignment, DueDateCommitment, DesignAttempt, ArkaSubmission,
      DesignFile, DesignChangeRequest,
      TaskTemplate, TaskTemplatePhase, TaskTemplateTask,
  )
  ```
  No vendor model appears. So there is no admin changelist, no "Delete selected" action, no
  change-form delete button.
- **Management command:** none deletes vendors.
- **Shell-only:** yes, `Vendor.objects.filter(...).delete()` works from a Django shell.
  Because every historical FK is `SET_NULL` and nothing is `PROTECT`, such a delete would
  **succeed silently** and null the vendor out of every `BOQItem.make_preference`,
  `BOQItem.ordered_vendor`, `DeliveryChallan.vendor` and `PaymentRequest.vendor` — and
  `CASCADE`-delete the `VendorBrand` rows. Nothing in the database would refuse it.

The only `.delete()` on anything vendor-shaped in the codebase is inside `_save_vendor_brands`,
and it deletes brands, not vendors:

```python
    vendor.brands.all().delete()
    for name, cat_id in zip(brand_names, brand_cats):
```

**Stop condition 1 is not triggered** — there is no screen-reachable hard-delete.

### What a `vendor_edit` name change does to existing records

`vendor_edit` calls `form.save()` on the live row. Historical records point at that row by FK
and **read the name live at render time**, so a rename retroactively rewrites what those
records display.

Concretely, after renaming vendor #7 from "Acme Supplies" to "Acme Trading Pvt Ltd":

- `projects/delivery_challan_detail.html`:
  `{% if challan.vendor %}{{ challan.vendor.name }}{% else %}…{% endif %}` → now shows
  **"Acme Trading Pvt Ltd"** on a challan raised months earlier against "Acme Supplies".
- `projects/payment_request_detail.html`: `{{ pr.vendor.name|default:"—" }}` → same.
- `projects/my_documents.html`, `projects/project_overview.html` (both the DC table and the
  PR table, plus the confirm-payment modal's `data-vendor`) → same.
- `dashboard/finance.html`: `{{ pr.vendor }}` (uses `__str__`, which returns `self.name`) →
  same.

### Is the vendor name snapshotted anywhere at write time?

**Almost nowhere. Two exceptions, both partial:**

1. **`_boq_snapshot()` — a real snapshot.** At each BOQ workflow transition it copies the
   name into stored JSON:
   ```python
   rows = list(boq.items.values(
       'serial_no', 'category', 'description', 'uom',
       'boq_quantity', 'ordered_quantity',
       'make_preference_id', 'make_preference__name', 'ordered_vendor__name',
   ))
   ...
           row['make_brand_label'] = (
               brand_map.get((vid, cat)) or       # category-specific brand
               brand_map.get((vid, None)) or      # unscoped brand for this vendor
               row.get('make_preference__name')   # company name fallback
           )
   ```
   `boq_history.html` renders `{{ item.ordered_vendor__name }}` from that JSON, so **BOQ
   history is immune to a rename.** This is the correct pattern and the only place it is
   applied.

2. **`log_activity` on DC creation — a frozen sentence, not a field.**
   ```python
   vendor_name = vendor.name if vendor else 'Unknown Vendor'
   log_activity(
       project, profile,
       f"SCM created Delivery Challan {dc_number} for {vendor_name}",
       entity_type='DeliveryChallan', entity_id=challan.pk,
   )
   ```
   The name is baked into the activity-log text at write time. So the activity log and the
   challan detail page will **disagree** after a rename.

Neither `DeliveryChallan` nor `PaymentRequest` has a `vendor_name` column. Confirmed by their
full field lists — `DeliveryChallan` has `vendor` (FK), `po_number`, `issued_from_warehouse`,
`dc_number`, `dc_date`, `expected_delivery_date`, `status`, `notes`, `created_by`,
`created_at`; `PaymentRequest` has `vendor` (FK), `boq_item`, `invoice_number`,
`invoice_document_name/url/path`, `amount`, `note`, `requested_by`, `requested_date`,
`status`, `payment_date`, `payment_reference`, `confirmed_by`. No snapshot field on either.

**Everything except BOQ history reads the vendor name live through the FK.**

---

## V6 — Whether deactivation means anything

# **YES — deactivation is meaningful.**

Both selection points filter on `is_active`, in both directions (dropdown *and* POST resolve).

### 1. SCM payment-request modal

The dropdown data, built in `dashboard_scm`:

```python
    # All active vendors for the raise-payment-request vendor dropdown
    scm_vendors = list(
        Vendor.objects.filter(is_active=True).order_by('name')
        .values('id', 'name')
    )
```

Rendered in `dashboard/scm.html`:

```django
  var allVendors   = {{ scm_vendors|safe }};
  ...
        var vendorSel = document.getElementById('scmPaymentVendorSelect');
        vendorSel.innerHTML = '<option value="">— Select Vendor —</option>';
```

And the POST handler re-applies the filter — a client cannot post an inactive vendor id:

```python
    vendor = get_object_or_404(Vendor, pk=vendor_id, is_active=True)
```

### 2. Delivery Challan create form

```python
@login_required
@role_required(['SCM'])
def create_delivery_challan(request, project_id):
    ...
    vendors = Vendor.objects.filter(is_active=True).order_by('name')
```

Rendered in `projects/delivery_challan_create.html`:

```django
          <select name="vendor_id" class="form-select" required>
            ...
            {% for v in vendors %}
```

POST resolve:

```python
    vendor = None
    if vendor_id:
        try:
            vendor = Vendor.objects.get(pk=vendor_id, is_active=True)
        except Vendor.DoesNotExist:
            pass
```

### 3. Also filtered (not asked, but worth recording)

- `project_overview` DC vendor dropdown:
  ```python
  dc_vendors = Vendor.objects.filter(is_active=True).order_by('name') if role == 'SCM' else []
  ```
- The BOQ Make/Preference dropdown, `_build_vendors_by_category()` — three separate
  `is_active=True` filters:
  ```python
  for v in Vendor.objects.filter(is_active=True).prefetch_related('categories'):
  ...
  for vb in (VendorBrand.objects
             .filter(vendor__is_active=True)
  ...
  for v in (Vendor.objects
            .filter(is_active=True)
            .exclude(pk__in=vendors_with_brands)
  ```
- `seed_opex_test_data`: `Vendor.objects.filter(is_active=True).order_by('pk').first()`

**Every single write-side vendor selection in the product respects `is_active`.** The only
unfiltered queryset is `vendor_list`'s own, which deliberately shows both so an inactive
vendor can be reactivated:

```python
    vendors = Vendor.objects.prefetch_related('categories').order_by('-is_active', 'name')
```

The "no delete" decision therefore rests on a working mechanism, not on a decorative flag.

---

## V7 — Data

**Source: local dev database `solarpms_local` on `localhost`.** These are *not* Railway
production numbers — I had no production credentials in this session, and this DB is
small enough that it is clearly seeded/dev data. Treat the shape as indicative, not the
magnitudes.

```
DB HOST=localhost NAME=solarpms_local

Vendor         : 2
VendorCategory : 6
VendorBrand    : 0
Vendor active  : 2
Vendor inactive: 0

DeliveryChallan rows total : 6
  with vendor set          : 6
PaymentRequest rows total  : 2
  with vendor set          : 2

Vendors referenced by >=1 DC or PR: 2
  id=1    active=True  dc=1   pr=2   name=horizon renewable power
  id=2    active=True  dc=5   pr=0   name=horizon

CATEGORIES: BOS(id=4), Inverter(id=3), Other(id=6), Services(id=5), Solar Modules(id=1), Structure(id=2)
```

Reading of that:

- **2 vendors, both active, both referenced.** 100% of vendors carry history. Every DC and
  every PR has a vendor. `SET_NULL` orphaning would be total, not marginal.
- **0 `VendorBrand` rows.** The entire brand subsystem — `_save_vendor_brands`, the dynamic
  brand rows in `vendor_form.html`, the `make_brand` half of `_build_vendors_by_category`,
  the `brand_map` resolution in `_boq_snapshot` — is built and unexercised. Every BOQ
  Make/Preference dropdown is currently taking the company-name fallback branch.
- The two vendor names, `horizon renewable power` and `horizon`, are Horizon's own company
  name — dev placeholders, not a real vendor master. **The real vendor master does not exist
  yet in this database.** That is the actual gap behind the request, and it is a data-entry
  gap, not a permissions one.
- `VendorCategory` is seeded with 6 sensible categories.

---

## V8 — Blast radius of granting SCM access

**SCM already has access, so nothing needs to be widened and the blast radius is nil.**

Answering the question as asked anyway — *do these four views share a permission helper,
frozenset or decorator pattern with anything else?*

**No. The gate is four hand-written copies of one literal tuple, and nothing else in the
codebase uses that tuple.** Every occurrence of an `('SCM', 'Admin')`-shaped literal, repo-wide:

```
projects/permissions.py:36:PORTFOLIO_VIEW_ROLES = frozenset({'CEO', 'Finance', 'SCM', 'Admin'})
projects/permissions.py:221:BOQ_PORTFOLIO_READ_ROLES = frozenset({'SCM', 'Admin', 'CEO'})
projects/tests_permissions.py:352:        for role in ('SCM', 'Admin', 'CEO'):
projects/views.py:536:    elif role in ('CEO', 'Finance', 'SCM', 'Admin', 'System Admin', 'BD'):
projects/views.py:5461:    if profile.role not in ('SCM', 'Admin'):   <- vendor_list
projects/views.py:5515:    if profile.role not in ('SCM', 'Admin'):   <- vendor_add
projects/views.py:5559:    if profile.role not in ('SCM', 'Admin'):   <- vendor_edit
projects/views.py:5603:    if profile.role not in ('SCM', 'Admin'):   <- vendor_toggle_status
projects/views.py:8698:    if role in ('Finance', 'PM', 'SCM', 'Admin'):
```

Lines 5461/5515/5559/5603 are the four vendor views. The two `permissions.py` frozensets
(`PORTFOLIO_VIEW_ROLES`, `BOQ_PORTFOLIO_READ_ROLES`) are **different sets** — both include
`'CEO'`, and the vendor views do not reference either. `views.py:536` and `views.py:8698` are
different sets again and are unrelated code paths.

**Explicitly stated, because you asked for it stated and not implied:**

> The vendor permission gate is narrow and isolated. It is four inline `if` statements
> against a two-element literal tuple, shared with no other screen, no helper, no frozenset
> and no decorator. Changing that tuple changes exactly these four vendor screens and
> nothing else in the product. There is no screen anywhere that a role would gain by being
> added to it.

**Stop condition 2 is not triggered.**

The one caveat, which is about maintenance rather than blast radius: because the tuple is
duplicated four times rather than named once, a future change to vendor access has four
places to get right, and there is no test (see V9) that would catch three-of-four.

---

## V9 — Tests

**No test anywhere covers vendor access, vendor creation, vendor editing or vendor
deactivation.**

- No test file references `vendor_list`, `vendor_add`, `vendor_edit` or `vendor_toggle_status`
  by name.
- No test issues a client request to `/projects/vendors/` or any sub-path. The only `vendors/`
  matches in Python are in `urls.py` and the three `render()` calls in `views.py`.
- No test asserts anything about `is_active` filtering on a vendor dropdown.
- No test covers `VendorForm`'s GST regex (`clean_gst_number`) or its MSME cross-field rule
  (`clean`), despite both being real validation logic.
- No test covers `_save_vendor_brands` or `_build_vendors_by_category`.

`Vendor` appears in tests only as an inert fixture for payment-request and access-isolation
scenarios:

```
projects/tests_access_isolation.py:58:    Vendor,
projects/tests_access_isolation.py:442:        vendor = Vendor.objects.create(name='Acme Supplies')
projects/tests_access_isolation.py:452:        vendor = Vendor.objects.create(name='Acme Supplies')
projects/tests_residential_baseline.py:80:    UserProfile, Vendor,
projects/tests_residential_baseline.py:876:        vendor = Vendor.objects.create(name='Sunrise Traders',
projects/tests_residential_baseline.py:914:        vendor = Vendor.objects.create(name='V2', contact_person='X', phone='9000000002')
projects/tests_residential_baseline.py:1555:        vendor = Vendor.objects.create(name='Sunrise', contact_person='R',
```

(The other grep hits for "vendor" in test files — `tests_current_phase.py:487`,
`tests_opex_activation.py:611`, `tests_opex_manual_dates.py:92`,
`tests_opex_template_correction.py:13,133` — are all prose about the removed
`Inspection — Factory / Vendor` OPEX template task, unrelated to the vendor master.)

**Vendor test coverage is zero.**

---

## Is add / edit / deactivate / no-delete the right shape?

**Yes, and it is already built. Do not add a delete.** The findings support the intended
decision rather than arguing against it, for three reasons:

1. **Every historical FK is `SET_NULL`, and none is `PROTECT`.** A hard delete would not be
   refused by the database — it would succeed and silently null the vendor out of every
   `BOQItem.make_preference`, `BOQItem.ordered_vendor`, `DeliveryChallan.vendor` and
   `PaymentRequest.vendor`, turning historical records into `default:"—"` in six templates
   with no trace of what they used to say. `PROTECT` would at least have refused; `SET_NULL`
   quietly shreds. The absence of a delete screen is what is currently keeping that from
   being reachable.
2. **On this data every vendor is referenced** (2 of 2, carrying 6 DCs and 2 PRs between
   them). There is no such thing as a safely-deletable vendor here.
3. **Deactivation already does the job a delete would be reached for.** It removes the vendor
   from all four selection surfaces and keeps every historical row intact and legible.

The shape is correct as designed and as implemented. The gap is not in the shape.

---

## What is actually broken, and was not fixed

Recorded per MODE. **Nothing below was changed.** None is a stop condition.

**1 — `'System Admin'` cannot reach vendor management.** `('SCM', 'Admin')` excludes
`'System Admin'`, which is a distinct `ROLE_CHOICES` value. A System Admin gets a bare
`HttpResponseForbidden()` on all four views with no message. Whether that is intended is a
product call, but the docstring's phrase "SCM and Admin only" reads as though it covers
administrators generally, and it does not.

**2 — `Admin` has access with no way to use it.** All four views admit `'Admin'`, but no
navbar item, Admin Panel screen or dashboard renders a single link to `vendor_list`. The only
rendered route is the SCM dashboard button, and `dashboard_scm` is `@role_required(['SCM'])`.
An Admin must know and type the URL. This is a dead half of the permission grant.

**3 — Renaming a vendor rewrites history on five screens.** `vendor_edit` mutates the live
row and every consumer except BOQ history reads the name through the FK at render time. A
rename retroactively changes what `delivery_challan_detail`, `payment_request_detail`,
`my_documents`, `project_overview` and `dashboard/finance` say a past challan or invoice was
raised against. `_boq_snapshot` is the one place that got this right. The mismatch is made
visible by the DC activity log, which *does* freeze the name at write time
(`f"SCM created Delivery Challan {dc_number} for {vendor_name}"`) — so after a rename the
activity log and the challan page will state two different vendors for the same event. Not
urgent at 2 vendors; it gets worse the moment a real vendor master is entered and someone
corrects a typo in a name.

**4 — The permission tuple is duplicated four times.** No helper, no frozenset, no decorator
(V8). Combined with zero test coverage (V9), a future change to vendor access can be applied
to three views out of four and nothing will catch it.

**5 — Zero test coverage on a subsystem with real validation logic.** `VendorForm`'s GST
regex and MSME cross-field rule, `_save_vendor_brands`'s delete-and-recreate, and every
`is_active` dropdown filter are all untested. The `is_active` filters are what V6's "yes"
depends on; nothing would catch one being dropped.

**6 — `VendorBrand` is fully built and entirely unused (0 rows).** Not a defect, but worth
knowing before anyone reasons about the brand code paths: `_build_vendors_by_category`'s
brand branches and `_boq_snapshot`'s `brand_map` have never executed against real data.

**7 — `create_delivery_challan` swallows an inactive vendor id.** In the POST resolve,
`except Vendor.DoesNotExist: pass` leaves `vendor = None` and the challan saves **without a
vendor** rather than rejecting the submission. This is deliberate for a blank selection (the
comment on the adjacent warehouse block says an empty selection is a valid submission), but it
means a vendor deactivated between GET and POST produces a silently vendor-less challan
instead of an error. The payment-request path, by contrast, uses
`get_object_or_404(..., is_active=True)` and 404s. Two different answers to the same race.

---

## Hard stop

Audit complete. No application code, template, URLconf, permission helper or migration was
touched; `VENDOR_ACCESS_AUDIT.md` is the only artefact. No build is proposed. Pending
sign-off.
