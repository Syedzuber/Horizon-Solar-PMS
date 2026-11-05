# TD-A — Tender-wide documents: read-only audit

- **Date:** 2026-11-05
- **Branch / commit:** `main` @ `96b67c06c20d79ec57c6a5ab4b0fad1b0feaf153`. After `git fetch origin`, `HEAD` = `origin/main`.
- **Mode:** read-only. No application file was changed. This session wrote only to this file and to `SECONDARY_FINDINGS.md`.
- **Data source:** every data finding below comes from **`solarpms_local`, a restore of the 31 Oct 2026 production dump** (`railway_31-oct.dump`), at migration `0134_notification_event_recipient`. That migration is the newest file in `projects/migrations/`. Production may have moved since 31 Oct.
- **Must be re-checked on production before build:** D1–D7 in §0.

---

## 0. Data findings that need a production re-check

| # | Finding (local, 31 Oct restore) | Source |
|---|---|---|
| D1 | Live sites without a Program: OPEX 0 of 213, Residential 42 of 42, CAPEX 0 live sites (2 deleted) | Q1 |
| D2 | No site sits under more than one Program, and no SiteGroup membership crosses tenders | Q2, Q2b |
| D3 | Programs with zero live sites: one CAPEX Program (id 3), zero OPEX | Q3 |
| D4 | PMs and coordinators per Program (usernames) | Q4, Q6 |
| D5 | Site Engineers holding tasks per Program: IPGCLG 9, RUDEH 7, the rest 0 | Q7 |
| D6 | 54 live `ProjectDocument` rows on 20 IPGCLG sites, but only 16 distinct file names; 2 file names appear on more than one OPEX site | Q9, Q11 |
| D7 | Which private buckets are configured in production (`SUPABASE_DELIVERY_BUCKET`, `SUPABASE_BILLS_BUCKET`, `SUPABASE_DESIGN_BUCKET`). Not checkable from here: they are Railway environment variables. | T5 |

---

## Pre-flight

```
$ git fetch origin            # exit=0
$ git rev-parse HEAD origin/main
96b67c06c20d79ec57c6a5ab4b0fad1b0feaf153
96b67c06c20d79ec57c6a5ab4b0fad1b0feaf153
```

Every database session opened with:

```
SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;  -> default_transaction_read_only = on
database = solarpms_local
```

---

## T1 — Program model and how sites link to it

### T1.1 The Program model — `projects/models.py:218-302`

```python
218	class Program(models.Model):
219	    """Parent record grouping multiple sites under one OPEX tender or one multi-site
220	    CAPEX contract. Each child site remains an ordinary Project (via Project.program)
...
232	    PROGRAM_TYPE_CHOICES = [
233	        ('OPEX',  'RESCO'),
234	        ('CAPEX', 'CAPEX'),
235	    ]
237	    STATUS_CHOICES = Project.STATUS_CHOICES
239	    program_type             = models.CharField(max_length=20, choices=PROGRAM_TYPE_CHOICES)
240	    name                     = models.CharField(max_length=200)      # e.g. "IPGCL Delhi Tender"
241	    client_name              = models.CharField(max_length=200)
242	    status                   = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
249	    short_tender_code        = models.CharField(max_length=20, blank=True, default='')
...
280	    created_at               = models.DateTimeField(auto_now_add=True)
281	    updated_at               = models.DateTimeField(auto_now=True)
282	    created_by               = models.ForeignKey(
283	        User, related_name='created_programs', on_delete=models.PROTECT, null=True, blank=True,
284	    )
285	    is_deleted               = models.BooleanField(default=False)
286	    deleted_at               = models.DateTimeField(null=True, blank=True)
```

The Program has no PM or coordinator field of its own.

### T1.2 How a site links: one nullable FK — `projects/models.py:55-65`

```python
55	    # First and only PARENT FK on Project. Nullable/forward-only: every pre-existing
56	    # project stays program=null (no backfill). on_delete=PROTECT so a Program can
57	    # never cascade-delete or orphan its child sites (see Program soft-delete rules).
58	    # Type-match / Residential-exclusion invariants are enforced in _validate_program_link().
59	    program                   = models.ForeignKey(
60	        'Program',
61	        null=True,
62	        blank=True,
63	        on_delete=models.PROTECT,
64	        related_name='sites',
65	    )
```

The invariants are checked on every save. `projects/models.py:189-199`:

```python
189	        if self.program_id is None:
190	            return
192	        if self.project_type == 'Residential':
193	            raise ValidationError('Residential projects cannot be linked to a Program.')
194	        program_type = self.program.program_type
195	        if self.project_type != program_type:
196	            raise ValidationError(
197	                f'Type mismatch: a {self.project_type} site cannot belong to a '
198	                f'{program_type} Program.'
```

### T1.3 Can a site belong to more than one Program? **No.**

`Project.program` is a single FK. The other Program references in the models are not site memberships:

| Field | Location | What it records |
|---|---|---|
| `VendorOrderProgram.program` | `models.py:3440` | the tenders an order was sized against (editable intention, not membership) |
| `SiteGroup.program` | `models.py:6369` | a procurement or QC batch inside one tender |
| `ApprovalRequest.programs` (M2M) | `models.py:7547` | the scope of an approval request |

SiteGroup membership is refused across tenders. `projects/design_views.py:8411-8413`:

```python
8411	        if project.program_id != group.program_id:
8412	            refused.append(f'{project.project_id}: belongs to a different tender.')
8413	            continue
```

Data (Q2, Q2b): **no rows** are linked to more than one Program, and **no rows** have a cross-tender SiteGroup membership.

```
### Q2 sites linked to more than one Program (Project.program is a single FK; checked anyway)
project_type | sites_with_gt1_program
(no rows)
### Q2b SiteGroup memberships whose group sits under a different Program than the site
project_type | cross_tender_memberships
(no rows)
```

**The stop condition was not triggered.**

### T1.4 Who writes `Project.program`

The only view-layer writer is `create_opex_site`, at creation. `projects/views.py:5196-5198`:

```python
5196	        site = form.save(commit=False)
5197	        site.program = program
5198	        site.project_type = 'OPEX'          # forced — never user-selectable here
```

There is no reassignment path:
- `ProjectCreateForm.Meta.fields` (`forms.py:359-373`) has no `program` field.
- `ProjectAdmin` fieldsets (`admin.py:160-188`) do not list `program`.
- A grep for `.program = ` / `update(program` in non-test code finds only `views.py:5197`.
- Seed commands write it too (`seed_opex_test_data`, `seed_order_demo`, `seed_scm_handoff_data`).

### T1.5 What happens to the link on PM handover: **nothing**

`change_site_pm` writes `assigned_pm` only. `projects/site_pm.py:260-263`:

```python
260	        # Race: safe for the same reason — the row is locked. filter().update() rather
261	        # than save() so this writes the one column and cannot overwrite a field another
262	        # request changed on a stale instance the caller is holding.
263	        Project.objects.filter(pk=site.pk).update(assigned_pm=new_pm)
```

`hand_over_sites(..., program=None)` (`site_pm.py:432-479`) loops `change_site_pm`. Its `program` argument only labels the Handover notice (`site_pm.py:457`, `514`, `541-542`). So **a handover changes who passes `user_can_manage_program`, but never which Program a site belongs to.**

### T1.6 Data, broken down by project_type

```
### Q1 sites by project_type: live/deleted, with/without Program
project_type | live_sites | live_no_program | live_with_program | live_no_program_non_test | deleted_sites
CAPEX | 0 | 0 | 0 | 0 | 2
OPEX | 213 | 0 | 213 | 0 | 4
Residential | 42 | 42 | 0 | 42 | 11

### Q3 Program rows by program_type: zero live sites
program_type | programs_total | programs_deleted | live_programs_zero_live_sites
CAPEX | 1 | 0 | 1
OPEX | 5 | 0 | 0
```

There are no Residential Programs: the type does not exist in `PROGRAM_TYPE_CHOICES`.

```
### Q4 Program rows with PMs (usernames) and coordinators holding live sites under each
id | program_type | short_tender_code | status | is_deleted | live_sites | live_test_sites | live_sites_no_pm | pm_usernames | coordinator_usernames
1 | OPEX | TESTTENDER26 | Draft | False | 9 | 9 | 0 | nitesh |
2 | OPEX | MPUVL | Draft | False | 94 | 0 | 0 | nirankar | chandan
3 | CAPEX |  | Draft | False | 0 | 0 | 0 |  |
4 | OPEX | IPGCLC | Draft | False | 86 | 0 | 0 | anand |
5 | OPEX | IPGCLG | Draft | False | 23 | 0 | 0 | mehtab | akhil, narendra
6 | OPEX | RUDEH | Active | False | 1 | 0 | 0 | kpraveen |

### Q5 Program creator usernames
id | short_tender_code | created_by
1 | TESTTENDER26 | nitesh
2 | MPUVL | nirankar
3 |  | mehtab
4 | IPGCLC | anand
5 | IPGCLG | anand
6 | RUDEH | kpraveen

### Q6 role + active flags of every user named in Q4/Q5
username | role | profile_active | user_active
akhil | Project Coordinator | True | True
anand | PM | True | True
chandan | Project Coordinator | True | True
kpraveen | PM | True | True
mehtab | PM | True | True
narendra | Project Coordinator | True | True
nirankar | PM | True | True
nitesh | PM | True | False

### Q10 PM / coordinator holding sites in more than one Program
(no rows)
```

---

## T2 — Who can view a Program and its sites today

### T2.1 Program pages are role-gated before any object check — `projects/views.py`

```python
4993	@login_required
4994	@role_required(['Admin', 'PM', 'CEO'])
4995	def program_list(request):

5044	@login_required
5045	@role_required(['Admin', 'PM', 'CEO'])
5046	def program_detail(request, pk):
...
5049	    program = get_object_or_404(Program, pk=pk, is_deleted=False)
5050	    if not _can_access_program(request, program):
5051	        raise Http404

5070	@role_required(['Admin', 'PM'])            # program_create
5092	@role_required(['Admin', 'PM'])            # program_edit
5135	@role_required(['Admin'])                  # program_delete
5229	@role_required(['Admin', 'PM'])            # opex_site_create
5444	@role_required(['Admin', 'PM'])            # opex_site_bulk_upload
5580	@role_required(['Admin', 'PM'])            # opex_site_bulk_template
```

`role_required` refuses any role that is not listed, with a 403. `projects/decorators.py:166-177`:

```python
166	            try:
167	                role = request.user.profile.role
168	            except Exception:
...
174	                return _forbidden(request)
175	            if role not in allowed_roles:
176	                return _forbidden(request)
177	            return view_func(request, *args, **kwargs)
```

The object gate is a thin wrapper. `projects/views.py:4984-4990`:

```python
4984	def _can_access_program(request, program):
...
4990	    return user_can_access_program(request.user, program)
```

### T2.2 The permissions.py helpers — `projects/permissions.py`

```python
542	def user_can_manage_program(user, program):
...
556	    profile = getattr(user, 'profile', None)
557	    if profile is None:
558	        return False
559	    for site in program.sites.filter(is_deleted=False):
560	        if user_can_manage_project(user, site):
561	            return True
562	    return False

567	PROGRAM_PORTFOLIO_ROLES = frozenset({'Admin', 'CEO'})

584	def user_can_access_program(user, program, *, handed_over_program_ids=None):
...
606	    profile = getattr(user, 'profile', None)
607	    if profile is None:
608	        return False
609	    if profile.role in PROGRAM_PORTFOLIO_ROLES:
610	        return True
611	    if user_can_manage_program(user, program):
612	        return True
613	    if program.created_by_id != user.id:
614	        return False
615	    if handed_over_program_ids is not None:
616	        return program.pk not in handed_over_program_ids
617	    return not (profile.pm_site_assignments
618	                .filter(valid_to__isnull=False, site__program=program)
619	                .exists())
```

`user_can_manage_project` is the canonical PM-or-coordinator check (`permissions.py:61-66`):

```python
61	    profile = getattr(user, 'profile', None)
62	    if profile is None:
63	        return False
64	    if project.assigned_pm == profile:          # PM authority — always checked, never gated
65	        return True
66	    return project.coordinators.filter(pk=profile.pk).exists()  # additive coordinator authority
```

### T2.3 What this means for each role

| Role | Program pages (`program_list` / `program_detail`) | Sites under the Program (`user_can_view_project`) |
|---|---|---|
| Admin, CEO | yes, every Program | yes (`PORTFOLIO_VIEW_ROLES`, `permissions.py:36`) |
| PM | only Programs they manage, or created and have not handed over | own sites only |
| **Project Coordinator** | **no: 403 at `role_required`, even though `user_can_manage_program` would admit them** | own sites only |
| **SCM, Finance** | **no (403)** | yes, portfolio |
| **System Admin** | **no (403; already logged at SECONDARY_FINDINGS.md:182)** | yes (`permissions.py:306-307`) |
| Site Engineer | no (403) | sites where they hold a task (`permissions.py:322-335`) |

On the local data, the coordinators who hold sites but cannot open the Program page are `chandan` (MPUVL) and `akhil` and `narendra` (IPGCLG) (Q4, Q6).

### T2.4 Other program-scoped pages and their gates

| View | Gate |
|---|---|
| `design_views.design_head_sites` (`/programs/<pk>/design/`) | `user_has_design_head_authority` |
| `design_views.design_bulk_allocate` | `user_has_design_head_authority` |
| `design_views.design_tender_dashboard` | `user_has_design_head_authority` |
| `design_views.design_qc_dashboard` | `user_can_view_design_qc_dashboard` |
| `design_views.design_quality_analytics` | `user_is_design_head` |
| `design_views.site_group_list` / `site_group_create` | `user_can_view_site_groups` / `user_can_manage_site_groups` |
| `order_views.program_vendor_order_list` | `user_can_view_program_vendor_orders(user, program)` |

---

## T3 — Existing models that store an uploaded file

### T3.1 Inventory

| Model (`models.py` line) | Bucket | Path pattern | URL | Opens logged? | Size / type validation |
|---|---|---|---|---|---|
| `ProjectDocument` (656) | **public** `SUPABASE_BUCKET` | `project-documents/{project_id}/{uuid}_{filename}` (`views.py:12187-12190`) | **public URL stored** in `file_url` (`views.py:12194-12197`) | no | `_validate_upload_file`: extension, 20 MB, MIME claim (`views.py:12031-12046`) |
| `TaskAttachment` (1456) | public | built in `views.py:12336-12350` | public URL stored | no | same |
| `DeliveryChallanAttachment` (1786) | private `SUPABASE_DELIVERY_BUCKET` | `{project_id}/challan|grn-document/{uuid}.{ext}` | signed on click, 300 s | **yes** (`dc_document_opened`) | `validate_dc_document`: PDF/JPG/PNG, 20 MB, MIME, **magic bytes** |
| `DCLineDamagePhoto` (1893) | private delivery | `{project_id}/grn-damage/{uuid}.{ext}` | signed on click, 300 s | **yes** (`dc_damage_photo_opened`) | jpg/png, then `validate_dc_document` |
| `VendorOrderDocument` (3511) | public | bucket + path; no URL stored | **public**, built by `vendor_order_document_url` | no | `_validate_upload_file` |
| `DesignSubmission` (3584) | public | legacy `file_url` / `supabase_path` | public | no | — |
| `ChecklistItemCompletion.photo_*` (4689) | public | `photo_supabase_path` | public | no | photo allow-list |
| `DesignAssignment.survey_file_*` (5361) | private design | `{project_id}/survey/{uuid}.{ext}` | signed on click (`design_views.py:2105`) | no | `validate_design_file`: extension, 25 MB, MIME |
| `DesignFile` (5895) | private design | `{project_id}/{kind}/{uuid}.{ext}` | signed on click (`design_views.py:4245`) | no | `validate_design_file` (+ `validate_cad_zip`) |
| `DesignFilePdf` (6000) | private design | extracted from the CAD zip | signed on click, 300 s | **yes** (`site_drawing_opened`) | `%PDF-` magic on each entry (`design_storage.py:545`) |
| `PunchPointEvent.photo_*` (6860) | private bills | `{project_id}/punch-point/...` | signed on click (`qaqc_views.py:1786`) | no | `validate_visit_photo` |
| `CodRecord.pdf_*` (7124) | private bills | `{project_id}/...` | signed on click (`cod_views.py:642`) | no | `%PDF-` magic (`cod_views.py:244`) |
| `QaqcVisitAnswer.photo_*` (7346) | private bills | `{project_id}/qaqc-visit/...` | signed on click (`qaqc_views.py:1568`) | no | `validate_visit_photo` (extension + MIME, no magic) |
| `ApprovalAttachment` (7743) | public | bucket + path | **public**, `vendor_order_document_url` (`approval_views.py:1773`) | no | `_validate_upload_file` |
| `ContractorBillDetail.pdf_*` (8028) | private bills | `{project_id}/bill/{uuid}.pdf` | signed, 900 s (`bill_storage.py:150-169`) | no | `validate_bill_pdf`: `%PDF-` magic |
| `ContractorAttendance.photo_*` (8610) | private bills | `{project_id}/...` | signed on click (`attendance_views.py:436`) | no | photo allow-list |

Open logging, by grep for `action_code='..._open...'` in non-test code:

```
projects/site_drawings_views.py:311:                 action_code='site_drawing_opened')
projects/views.py:14357:                 action_code='dc_document_opened')
projects/views.py:14457:                 action_code='dc_damage_photo_opened')
```

### T3.2 Best candidate 1 — `DeliveryChallanAttachment`: model, validator, signer, open, withdraw

It is the closest fit. It is private, the files are read by Site Engineers, it takes exactly PDF/JPG/PNG with magic-byte checks, a link is signed on each click, every open is logged, and withdrawal needs a reason and deletes nothing.

Model, `projects/models.py:1810-1890` (excerpt):

```python
1813	    uploaded_by = models.ForeignKey(
1814	        'UserProfile', on_delete=models.PROTECT, related_name='dc_attachments_uploaded',
1815	    )
1816	    uploaded_at = models.DateTimeField(default=timezone.now)
1817	    original_name = models.CharField(max_length=255)   # as uploaded; never used in the path
1818	    bucket = models.CharField(max_length=100)          # SUPABASE_DELIVERY_BUCKET at write time
1819	    path = models.CharField(max_length=500, unique=True)  # within `bucket`; uuid filename
1820	    size = models.PositiveIntegerField()               # bytes
1821	    mime = models.CharField(max_length=100)            # canonical type stored on the object, not the browser's claim
1822	    no_prices_confirmed = models.BooleanField()        # the uploader's tick; CHECK holds it true
1824	    withdrawn_at = models.DateTimeField(null=True, blank=True)  # NULL = live
1825	    withdrawn_by = models.ForeignKey(
1826	        'UserProfile', on_delete=models.PROTECT, null=True, blank=True,
1827	        related_name='dc_attachments_withdrawn',
1828	    )
1829	    withdrawn_reason = models.TextField(blank=True, default='')  # mandatory once withdrawn
...
1847	            models.CheckConstraint(
1848	                condition=models.Q(no_prices_confirmed=True),
1849	                name='dc_attachment_no_prices_confirmed'),
...
1860	            models.CheckConstraint(
1861	                condition=((models.Q(withdrawn_at__isnull=True)
1862	                            & models.Q(withdrawn_by__isnull=True)
1863	                            & models.Q(withdrawn_reason=''))
1864	                           | (models.Q(withdrawn_at__isnull=False)
1865	                              & models.Q(withdrawn_by__isnull=False)
1866	                              & ~models.Q(withdrawn_reason=''))),
1867	                name='dc_attachment_withdraw_fields'),
1868	            models.CheckConstraint(
1869	                condition=(models.Q(withdrawn_at__isnull=True)
1870	                           | models.Q(withdrawn_at__gte=models.F('uploaded_at'))),
1871	                name='dc_attachment_withdrawn_after_uploaded'),
...
1881	    def save(self, *args, **kwargs):
1882	        if self.pk is not None:
1883	            raise AppendOnlyViolation(
...
1888	    def delete(self, *args, **kwargs):
1889	        raise AppendOnlyViolation(
```

Validator, `projects/delivery_storage.py:55-60` and `77-107`:

```python
55	DC_DOCUMENT_TYPES = {
56	    'pdf':  ('application/pdf', frozenset({'application/pdf'}), b'%PDF-'),
57	    'jpg':  ('image/jpeg', frozenset({'image/jpeg', 'image/pjpeg'}), b'\xff\xd8\xff'),
58	    'jpeg': ('image/jpeg', frozenset({'image/jpeg', 'image/pjpeg'}), b'\xff\xd8\xff'),
59	    'png':  ('image/png', frozenset({'image/png', 'image/x-png'}), b'\x89PNG\r\n\x1a\n'),
60	}
...
84	    if ext not in DC_DOCUMENT_TYPES:
85	        raise DeliveryStorageError(f'{name or "A file"}: must be a PDF, JPG or PNG.')
86	    stored_mime, accepted, magic = DC_DOCUMENT_TYPES[ext]
87	    size = getattr(file_obj, 'size', 0) or 0
88	    if size == 0:
89	        raise DeliveryStorageError(f'{name}: the file is empty.')
90	    if size > DC_DOCUMENT_MAX_BYTES:
...
95	    claimed = (getattr(file_obj, 'content_type', '') or '').split(';')[0].strip().lower()
96	    if claimed and claimed not in accepted | _GENERIC_MIME:
97	        raise DeliveryStorageError(f'{name}: this does not look like a .{ext} file.')
98	    try:
99	        file_obj.seek(0)
100	        head = file_obj.read(len(magic))
101	        file_obj.seek(0)
...
104	    if head != magic:
105	        raise DeliveryStorageError(
106	            f'{name}: the contents are not a .{ext} file, although the name says so.')
107	    return stored_mime
```

The size limit is a module constant, not a parameter. `delivery_storage.py:40`:

```python
40	DC_DOCUMENT_MAX_BYTES = 20 * 1024 * 1024
```

Signer, `projects/delivery_storage.py:164-181`. It never signs into any bucket but the configured one:

```python
170	    configured = delivery_bucket()
171	    if not configured or not attachment.bucket or not attachment.path:
172	        return None
173	    if attachment.bucket != configured:
174	        logger.warning(...)
176	        return None
177	    try:
178	        return get_design_file_url(attachment.bucket, attachment.path, expires_in)
```

Open view, `projects/views.py:14321-14358`. The link is minted first, then one row is logged:

```python
14337	    if not user_can_open_dc_document(request.user, project):
14338	        return refuse_site_view_404(request, project)
14339	    document = _dc_document_or_404(project, dc_id, attachment_id)
14342	    if not document.is_live and not user_can_manage_dc_documents(request.user, project):
14343	        raise Http404
14345	    url = dc_document_url(document)
14346	    if not url:
...
14351	    # No file name in the line (ruling Q3): the project timeline shows every row to
14352	    # everyone on the site, and an SCM/Admin open of a WITHDRAWN document would
14353	    # otherwise name it to a Site Engineer. The row still points at the attachment.
14354	    log_activity(project, request.user.profile,
14355	                 f'Opened a challan document on DC {document.challan.dc_number}',
14356	                 entity_type='DeliveryChallanAttachment', entity_id=document.pk,
14357	                 action_code='dc_document_opened')
14358	    return redirect(url)
```

### T3.3 Best candidate 2 — `DesignFile`: versioned supersede

It is the only file model with a supersede chain: `is_current` plus `superseded_by`. `projects/models.py:5952-5958`:

```python
5952	    is_current = models.BooleanField(default=True)
5953	    # Points at the DesignFile that replaced this one. SET_NULL so deleting the
5954	    # replacement never cascades away the row it superseded.
5955	    superseded_by = models.ForeignKey(
5956	        'self', null=True, blank=True, on_delete=models.SET_NULL,
5957	        related_name='supersedes',
5958	    )
...
5973	    class Meta:
5974	        ordering        = ['attempt', 'kind', 'version']
5975	        unique_together = ('attempt', 'kind', 'version')
```

The supersede write, inside one transaction. `projects/design_views.py:4175-4193`:

```python
4175	    with transaction.atomic():
4176	        previous = attempt.design_files.filter(kind=kind, is_current=True).first()
4177	        next_version = (attempt.design_files.filter(kind=kind)
4178	                        .aggregate(m=Max('version'))['m'] or 0) + 1
4180	        design_file = DesignFile.objects.create(
...
4188	            uploaded_by=profile, remarks=remarks, is_current=True,
4189	        )
4190	        if previous is not None:
4191	            previous.is_current    = False
4192	            previous.superseded_by = design_file
4193	            previous.save(update_fields=['is_current', 'superseded_by'])
```

This chain has no DB constraint for "one current per (attempt, kind)", and `previous` is read without `select_for_update`.

---

## T4 — The Drawings & BOQ card

- **Template:** `projects/templates/projects/_site_drawings_card.html` (111 lines), included at `project_overview.html:242`.
- **Context:** `site_drawings_views.site_drawings_context(project)`, called in `views.project_overview` (`views.py:11702-11703`).
- **Gate:** the page gate is `user_can_view_project` (`views.py:11154-11155`). The card adds an OPEX test through the context's `not_opex` state. Together these equal `user_can_view_site_drawings` (`permissions.py:1210-1226`):

```python
1226	    return user_can_view_project(user, project) and project.project_type == 'OPEX'
```

```python
11154	    if not user_can_view_project(request.user, project):
11155	        return refuse_site_view_404(request, project)
...
11696	    # DR-2 — the site drawings card: the released design's PDFs and BOQ quantities, for
11697	    # every viewer of an OPEX site. This page's user_can_view_project() gate above is the
11698	    # first half of user_can_view_site_drawings(); the context's `not_opex` state is the
11699	    # second, so the gate is not asked again (its queries would repeat the page's own).
...
11702	    from .site_drawings_views import site_drawings_context
11703	    site_drawings = site_drawings_context(project)
```

- **Closed by default:** a Bootstrap collapse without `show`, with `aria-expanded="false"`. There is no Alpine on the page: `x-data|x-show|@click` count is 0 in both `project_overview.html` and `program_detail.html`. Template lines 13-16, 25-26, 38-41, 57:

```django
13	CLOSED BY DEFAULT (DR-2c-1) — a deliberate exception to "overview blocks open by default",
14	using the Gantt card's mechanism: Bootstrap collapse without `show`, and aria-expanded
15	"false" on the .block-toggle button, which must stay in step or the chevron points the
16	wrong way on load. So everything a site needs to know without opening it lives in the
...
25	{% if site_drawings and site_drawings.state != 'not_opex' %}
26	<div class="card shadow-sm mb-3" id="site-drawings-card">
...
38	      <button class="btn btn-link btn-sm p-0 text-muted block-toggle"
39	              type="button" data-bs-toggle="collapse"
40	              data-bs-target="#siteDrawingsCollapse" aria-expanded="false"
41	              aria-controls="siteDrawingsCollapse">
...
57	  <div class="collapse" id="siteDrawingsCollapse">
...
64	              <a href="{% url 'site_drawing_open' d.pk %}" target="_blank" rel="noopener" class="text-break">{{ d.display_name }}</a>
```

- **Open logging:** `site_drawing_open` re-checks the gate, signs for 300 s (`DRAWING_LINK_SECONDS`, `site_drawings_views.py:68`) and logs one row per open. `projects/site_drawings_views.py:289-312`:

```python
289	    if not user_can_view_site_drawings(request.user, project):
290	        return refuse_site_view_404(request, project)
...
299	    try:
300	        url = get_design_file_url(pdf.bucket, pdf.path, expires_in=DRAWING_LINK_SECONDS)
301	    except DesignStorageError:
302	        logger.exception('site_drawing_open: could not sign DesignFilePdf %s.', pdf.pk)
303	        url = None
304	    if not url:
305	        messages.error(request, 'This drawing cannot be opened right now. Try again, or '
306	                                'contact Design if it keeps failing.')
307	        return redirect(reverse('project_overview', args=[project.project_id])
308	                        + '#site-drawings-card')
309	    log_activity(project, request.user.profile, f'Opened drawing {pdf.display_name}',
310	                 entity_type='DesignFilePdf', entity_id=pdf.pk,
311	                 action_code='site_drawing_opened')
312	    return redirect(url)
```

---

## T5 — Storage layer

**There is no single abstraction over Supabase.** There are two generations of code, and every module builds its own client call.

`settings.py:25-42`:

```python
25	SUPABASE_URL    = config('SUPABASE_URL', default='')
26	SUPABASE_KEY    = config('SUPABASE_KEY', default='')
27	SUPABASE_BUCKET = config('SUPABASE_BUCKET', default='solarpms-files')  # Bucket for all project/task files
31	SUPABASE_DESIGN_BUCKET = config('SUPABASE_DESIGN_BUCKET', default='Horizon-PMS-Design')
36	SUPABASE_BILLS_BUCKET = config('SUPABASE_BILLS_BUCKET', default='')
42	SUPABASE_DELIVERY_BUCKET = config('SUPABASE_DELIVERY_BUCKET', default='')
```

Two client builders:
- `supabase_storage.get_supabase_client()` (`supabase_storage.py:4-20`), used by the public bucket.
- `design_storage._client()` (`design_storage.py:119-126`), used by the private buckets.

The one shared chokepoint is the **signer**: `design_storage.get_design_file_url` (`design_storage.py:354-369`) mints every signed URL in the codebase. Its only `create_signed_url` call:

```
projects/design_storage.py:365:        res = _client().storage.from_(bucket).create_signed_url(path, expires_in)
```

Every direct `.storage.from_` / client caller (non-test):

```
projects/approval_views.py:366:        client = get_supabase_client()
projects/attendance.py:755:        _client().storage.from_(bucket).remove([path])
projects/bill_storage.py:110:        _client().storage.from_(bucket).upload(
projects/bill_storage.py:143:        _client().storage.from_(bucket).remove([path])
projects/cod_views.py:266:        _client().storage.from_(bucket).upload(
projects/cod_views.py:295:        _client().storage.from_(bucket).remove([path])
projects/delivery_storage.py:133:        _client().storage.from_(bucket).upload(
projects/delivery_storage.py:157:        _client().storage.from_(bucket).remove([path])
projects/delivery_storage.py:234:        _client().storage.from_(bucket).upload(
projects/delivery_storage.py:256:        _client().storage.from_(bucket).remove([path])
projects/design_storage.py:305:        _client().storage.from_(DESIGN_BUCKET).upload(
projects/design_storage.py:347:            client.storage.from_(bucket).remove([path])
projects/design_storage.py:365:        res = _client().storage.from_(bucket).create_signed_url(path, expires_in)
projects/design_storage.py:388:        return _client().storage.from_(bucket).download(path)
projects/management/commands/purge_deleted_files.py:40-41:  get_supabase_client() ... .remove([doc.supabase_path])
projects/management/commands/purge_deleted_files.py:51-52:  get_supabase_client() ... .remove([attach.supabase_path])
projects/order_views.py:416:            client.storage.from_(bucket).remove([path])
projects/order_views.py:449:        client = get_supabase_client()
projects/qaqc_views.py:924:        _client().storage.from_(bucket).upload(
projects/qaqc_views.py:949:        _client().storage.from_(bucket).remove([path])
projects/qaqc_views.py:969:        _client().storage.from_(bucket).remove([path])
projects/views.py:12056:    supabase_client.storage.from_(bucket).upload(      # _validate_and_upload
projects/views.py:12177:        client = get_supabase_client()                  # upload_project_document
projects/views.py:12329:        client = get_supabase_client()                  # upload_task_attachment
projects/views.py:12579:            client = get_supabase_client()
```

There are also three hand-built public URLs at `views.py:12195`, `12349` and `12600`, plus `supabase_storage.vendor_order_document_url`.

An S3 move would therefore touch about 10 modules: three uploaders, two client builders, one signer, and the public-URL builders. No `boto3`, `django-storages` or `AWS_` reference exists. `requirements.txt:12` is `supabase==2.31.0`.

---

## T6 — Void / withdraw / supersede patterns to mirror

### T6.1 `void_attendance` — `projects/attendance.py:435-469`

It locks the row, checks authority on the locked row, stamps who, when and why, and writes a revision row.

```python
445	    note = (note or '').strip()
446	    if not note:
447	        raise AttendanceRefused(VOID_NOTE_REQUIRED)
448	    with transaction.atomic():
449	        locked = _lock(entry)
...
454	        if not (user_can_change_attendance_today(by, locked)
455	                or user_can_correct_attendance(by, locked)
456	                or user_can_void_attendance_any_day(by, locked)):
457	            raise AttendanceRefused(NOT_CHANGEABLE)
458	        before = attendance_snapshot(locked)
459	        now = timezone.now()
460	        locked.is_voided = True
461	        locked.void_note = note
462	        locked.voided_by = by.profile
463	        locked.voided_at = now
464	        locked.updated_at = now
465	        locked.save(update_fields=['is_voided', 'void_note', 'voided_by', 'voided_at',
466	                                   'updated_at'])
467	        _revision(locked, ContractorAttendanceRevision.VOIDED, before, by.profile,
468	                  note=note, now=now)
```

Constraints, `projects/models.py:8658-8670`:

```python
8659	            models.CheckConstraint(
8660	                condition=models.Q(is_voided=False) | ~models.Q(void_note=''),
8661	                name='attendance_void_note_required'),
8663	            models.CheckConstraint(
8664	                condition=(
8665	                    (models.Q(is_voided=False) & models.Q(voided_by__isnull=True)
8666	                     & models.Q(voided_at__isnull=True))
8667	                    | (models.Q(is_voided=True) & models.Q(voided_by__isnull=False)
8668	                       & models.Q(voided_at__isnull=False))
8669	                ),
8670	                name='attendance_void_fields_together'),
```

### T6.2 DC document withdraw: a conditional `filter().update()` with no lock — `projects/views.py:14386-14408`

The reason must be at least 10 characters (`DC_DOCUMENT_WITHDRAW_REASON_MIN = 10`, `views.py:14362`). The feed line carries no reason and no file name.

```python
14386	    reason = (request.POST.get('withdrawn_reason') or '').strip()
14387	    if len(reason) < DC_DOCUMENT_WITHDRAW_REASON_MIN:
...
14392	    # RACE: two withdrawals of the same document at once. The withdrawn_at IS NULL term
14393	    # makes the update conditional in SQL, so exactly one of them stamps the row and the
14394	    # other updates nothing and is told so — never a second stamp over the first.
14395	    updated = (DeliveryChallanAttachment.objects
14396	               .filter(pk=document.pk, withdrawn_at__isnull=True)
14397	               .update(withdrawn_at=timezone.now(), withdrawn_by=profile,
14398	                       withdrawn_reason=reason))
14399	    if updated != 1:
14400	        messages.error(request, f'{document.original_name} was already withdrawn.')
14401	        return redirect(back)
...
14405	    log_activity(project, profile,
14406	                 f'Withdrew a challan document from DC {document.challan.dc_number}',
14407	                 entity_type='DeliveryChallanAttachment', entity_id=document.pk,
14408	                 action_code='dc_document_withdrawn')
```

### T6.3 Others found (not pasted)

- `cod_views.withdraw_cod` (`cod_views.py:418`): one active row per site, history kept (`CodRecord` docstring, `models.py:7091-7094`).
- `approvals.withdraw_approval_request` (`approvals.py:1521`).
- `ApprovalStep.superseded_by` (`models.py:7636`).

### T6.4 The current per-site document model **hard-deletes the stored file later**

`ProjectDocument` delete is a soft delete by the uploader or Admin, and the object is purged after `FILE_RETENTION_DAYS`. `projects/views.py:12272-12278`:

```python
12272	    if doc.uploaded_by != profile and profile.role != 'Admin':
12273	        return HttpResponseForbidden()
12275	    doc.is_deleted  = True
12276	    doc.deleted_at  = timezone.now()
12277	    doc.deleted_by  = profile
12278	    doc.save(update_fields=['is_deleted', 'deleted_at', 'deleted_by'])
```

The `role != 'Admin'` comparison is an inline role string. The proposed v1 ("no hard delete") must not copy this model.

---

## T7 — Content validation and HEIC

**Magic-byte checks exist**, by grep:

```
projects/bill_storage.py:88:    if head != b'%PDF-':
projects/checklist_pdf.py:137:    if not pdf.startswith(b'%PDF'):          # output check on a generated PDF, not an upload
projects/cod_views.py:244:    if head != b'%PDF-':
projects/delivery_storage.py:56-59:   DC_DOCUMENT_TYPES  (%PDF-, \xff\xd8\xff, \x89PNG\r\n\x1a\n)
projects/delivery_storage.py:104:    if head != magic:
projects/design_storage.py:410:PDF_MAGIC = b'%PDF-'      # CAD-zip PDF entries
```

`validate_dc_document` is the only upload validator that checks magic bytes for **all three** proposed types (PDF, JPG, PNG). The others check by extension and the browser's MIME claim only:
- `views._validate_upload_file` (`views.py:12031-12046`)
- `design_storage.validate_design_file` (`design_storage.py:148-177`). Its own comment says the MIME check is "A TYPO GUARD, NOT A SECURITY CONTROL" (`design_storage.py:84`).
- `qaqc_views.validate_visit_photo` (`qaqc_views.py:891-900`)

**HEIC is not accepted anywhere.** The only mentions are comments in `approval_forms.py`:

```python
87	#: What a proxy decision's evidence may be: a PDF, or a photo or screenshot (a WhatsApp
88	#: screenshot is PNG or JPEG). Not HEIC or WebP — views._validate_upload_file's MIME map
89	#: has neither (SECONDARY_FINDINGS, Approvals 2a-2).
...
114	    Naming image/jpeg is meant to make an iPhone hand over a HEIC photo converted to JPEG
115	    (not yet walked on a real iPhone); the extensions keep a desktop picker filtering by
```

A `.heic` file is refused by every validator's extension check. Nothing converts one.

Size settings: `settings.py:48-49` sets `DATA_UPLOAD_MAX_MEMORY_SIZE` / `FILE_UPLOAD_MAX_MEMORY_SIZE` = 100 MB. Per-feature limits are 20 MB (`views.MAX_FILE_SIZE_BYTES`, `views.py:311`; `DC_DOCUMENT_MAX_BYTES`; `BILL_PDF_MAX_BYTES`) and 25 MB (`MAX_DESIGN_FILE_BYTES`, `design_storage.py:52`).

---

## T8 — Where a new card fits on the project page

`project_overview.html` (2368 lines), top section:

```django
239	  </div>
240	</div>
241
242	{% include 'projects/_site_drawings_card.html' %}
243	{% include 'projects/attendance/_overview_card.html' %}
244
245	{# ── 2. Phase Progress (left 50%) + Payment Milestones (right 50%) ─────── #}
```

Further down are `{# ── 5. Project Documents ─── #}` (line 911, the per-site `ProjectDocument` card, not type-gated) and the file-preview modal (978).

Type-specific blocks are hidden two ways, both server-side:

1. A template test on the stored constant. `project_overview.html:300` `{% if project.project_type == 'Residential' %}` (payment milestones), line 1031-1102 (Gantt, Residential only), line 1284 `{% if is_assigned_pm and project.project_type == 'OPEX' %}`.
2. A context state from a Python builder, which the include tests. `_site_drawings_card.html:25` `{% if site_drawings and site_drawings.state != 'not_opex' %}`.

A "Tender documents" card fits naturally as a third include beside lines 242-243, with a context builder that returns nothing for a site without `program_id`. That covers Residential (never has a Program) and CAPEX (0 live sites have one) without testing `project_type` in the template.

The Program page (`program_detail.html`, 113 lines) has a stage breakdown card (66) and a "Child sites" card (73-111). A manage list would be a new card there.

---

## T9 — Risks and conflicts with the proposed v1

| # | Proposed v1 says | Evidence says | Severity |
|---|---|---|---|
| K1 | "Upload: Admin + PMs/coordinators … manage list on the Program page" | Program pages are `role_required(['Admin','PM','CEO'])` (T2.1). **Project Coordinators get a 403** although `user_can_manage_program` admits them. Locally 3 coordinators hold tender sites (`chandan`, `akhil`, `narendra`). SCM, Finance and System Admin cannot open the Program page either. | **High.** Either widen `program_list`/`program_detail`'s roles (a separate access change), or put the manage list somewhere coordinators can reach. |
| K2 | "CAPEX has no Program" | The model still allows `program_type='CAPEX'` (`models.py:232-235`); `ProgramForm` refuses it (`forms.py:1036-1050`, CAPEX-3), but the Django admin can still create one (SECONDARY_FINDINGS.md:582). One CAPEX Program row exists (id 3, 0 sites; Q3, Q4). | Medium. The feature must gate on `program.program_type == 'OPEX'` explicitly, not assume. |
| K3 | "Management only (PM, coordinators, SCM, Finance, CEO, Admin)" | `user_can_see_project_money` (`permissions.py:2872-2904`) is exactly that set **plus System Admin** (`MONEY_PORTFOLIO_ROLES`, `permissions.py:2722`). Design, Design Head, BD and a non-managing QA/QC engineer can see the site but are in neither proposed class's list explicitly. | Medium. Needs a ruling (R6). |
| K4 | "every open logged" | `log_activity(project, …)` writes to the site timeline, which every site viewer reads. DC ruling Q3 already keeps file names out of such lines (`views.py:14351-14353`). `MONEY_ACTIVITY_CODES` cannot be used to hide a Management-only open line: `tests_activity_money` fails if a code in that set is written without a money amount (`activity_codes.py:10-14`). `ActivityLog.project` is nullable (`models.py:1547-1548`), and `program_delete` already logs with `project=None, entity_type='Program'` (`views.py:5155-5159`). | Medium. Needs a ruling on where opens are logged and what the line says (R7). |
| K5 | "Max ~25 MB, contents checked server-side" | The magic-byte validator that fits (`validate_dc_document`) hard-codes 20 MB (`delivery_storage.py:40`, `90`). Every other non-design upload is 20 MB; design is 25 MB. | Low. Either use 20 MB, or extract the type/magic check so the limit is a parameter. |
| K6 | "Signed link per click" | Signed links already work for 4 private buckets through one signer (T5). But the delivery and bills buckets are **off until set** (`settings.py:36`, `42`, both `default=''`), and their production values cannot be read from here (D7). | Medium. The bucket choice decides whether the feature works on deploy. |
| K7 | "Supersede (latest shown, history kept)" | The only supersede chain (`DesignFile`) has no constraint for "one current per series" and reads `previous` without a lock (T3.3). A tender document also has no natural series key, unlike DesignFile's `(attempt, kind)`. | Medium. Needs an explicit series rule and a lock (R8). |
| K8 | "no prices may reach Site Engineers" | The "Site team" class is SE-readable. The SE-readable precedent (DC documents) holds a `no_prices_confirmed` CHECK and an SCM/Admin withdraw (T3.2). The current per-site `ProjectDocument` card has **no** such control, and any site viewer, SEs included, may upload to it (`views.py:12167`). | Medium. The tick should be mirrored for the Site-team class. |
| K9 | Card on "each site's project page" | Sites are Draft (98) or Active (115) locally (Q8). The Program page lists Cancelled and test sites (SECONDARY_FINDINGS.md:226). TESTTENDER26's 9 sites are all `is_test`. | Low. Ruling on Cancelled and test sites (R10). |
| K10 | Program-page manage list | `user_can_manage_program` loops sites with one query each. `program_detail` already costs 177 queries for a PM without access (SECONDARY_FINDINGS.md:227). Calling it once per upload or manage action adds the same cost. | Low. Use `manageable_projects_q` with `.filter(program=…).exists()` for the new helper, as one query. |
| K11 | "PMs who can't currently reach the Program page" | The creator fallback lets a creator who never held a seat in the tender keep Program access (`permissions.py:613-619`). Locally `anand` created IPGCLG (Q5) but holds no IPGCLG site (Q4): `user_can_access_program` admits them, while `user_can_manage_program` (the proposed upload rule) does not. | Low. Decide whether "can open the Program page" and "can upload" may differ (R4). |
| K12 | PM handover | Handover moves `assigned_pm` only (T1.5), so upload authority moves with the seat automatically when it derives from `user_can_manage_program`. Documents the outgoing PM uploaded stay. | None, if authority is derived. A stored "owner" field would break it. |
| K13 | Existing per-site uploads | 54 live `ProjectDocument` rows on 20 IPGCLG sites with only 16 distinct names, and 2 names repeated across sites (D6). These sit in the public bucket. | Information only. Migrating them is out of v1 scope. |

---

## Proposed rulings

### Supported by the evidence

- **R1. A document belongs to one Program; sites see it through `Project.program`.** This is a single FK (T1.2); no multi-Program sites (Q2) and no cross-tender groups (Q2b).
- **R2. OPEX only.** All 213 live OPEX sites have a Program, while Residential never does (`_validate_program_link`) (Q1). The feature should test `program.program_type == 'OPEX'`, given K2.
- **R3. Upload authority = Admin role OR `user_can_manage_program(user, program)`.** This should be a new `permissions.py` helper, written as one query over `manageable_projects_q(profile)` filtered by `program`. It derives from the seat, so it follows a handover (K12) and needs no stored owner.
- **R5. Site team audience = `user_can_view_site_drawings(user, project)`** (`permissions.py:1226`), the same audience as the Drawings & BOQ card.
- **R9. Validation = `validate_dc_document`'s type/magic table** (PDF, JPG, PNG with magic bytes, MIME claim as a typo guard). HEIC is refused, consistent with every other upload (T7).
- **R11. Card closed by default, with the DR-2c-1 Bootstrap mechanism and no Alpine** (T4). It renders only when `project.program_id` is set and the Program is OPEX, through a context builder (T8 pattern 2).
- **R12. Signed link minted per click** (300 s, the DC/drawing value) through `get_design_file_url`, with a pinned-bucket check like `dc_document_url` (T3.2). Log one row per open, written after the link is minted.
- **R13. Void = DC withdraw shape:** `voided_at` / `voided_by` / `void_note` all-or-none CHECK, a note of at least 10 characters, a conditional `filter(voided_at__isnull=True).update()`, no deletion of the row or the object, and `save()`/`delete()` guards as on `DeliveryChallanAttachment` (T6).

### Contradicted by the evidence

- **"Manage list on the Program page" for coordinators** (K1): coordinators cannot open that page today.
- **"CAPEX has no Program"** (K2): the model allows one, one row exists, and the admin can create more.
- **"Max ~25 MB" with the DC validator as-is** (K5): that validator is fixed at 20 MB.

### Left open (needs Zuber)

- **R4.** May a Program creator who manages no site there (K11) upload? Recommendation: no. Use upload = manage only.
- **R6.** Management-only audience: adopt `user_can_see_project_money` as-is, which **adds System Admin**? And confirm Design, Design Head, BD and non-managing QA/QC engineers see Site-team documents only.
- **R7.** Where uploads, voids and opens are logged: the site's timeline (feed line without a file name, per DC Q3), or Program-level rows (`project=None`, `entity_type='TenderDocument'`, the `program_delete` precedent), or both.
- **R8.** Supersede identity: replace "this document" by picking the row to supersede (no series key), or introduce a title/series key? Can the visibility class change on supersede? Needs a row lock on the superseded document.
- **R10.** Show the card on Cancelled sites and on `is_test` sites?
- **R14.** Which private bucket: `SUPABASE_DELIVERY_BUCKET` (SE-readable precedent, off until set), `SUPABASE_DESIGN_BUCKET` (always defaulted), or a new `SUPABASE_TENDER_BUCKET`? The path prefix must be Program-based (for example `program-{pk}/tender/{uuid}.{ext}`) because `build_design_path` takes a project id. Check the production environment first (D7).
- **R15.** Is the `no_prices_confirmed` tick required for the Site-team class only, or for both (K8)?
- **R16.** 20 MB or 25 MB (K5)?
- **R17.** Where the manage list lives if Program-page access is not widened (K1): a manage section on the site card, shown to those R3 admits, or a separate tender-documents page gated by the R3 helper.

---

## Recommended build split (one concern per session)

| Session | Concern | Scope |
|---|---|---|
| TD-0 (only if R17 chooses "widen") | Program page access | Add Project Coordinator (and System Admin, per SECONDARY_FINDINGS.md:182) to `program_list`/`program_detail`'s `role_required`, with `user_can_access_program` as the object gate. Tests. Nothing about documents. |
| TD-B1 | Model + storage + permissions, no UI | `TenderDocument` (Program FK PROTECT, visibility class, `original_name`, `bucket`, unique `path`, `size`, `mime`, `uploaded_by/at`, `supersedes`/`superseded_by`, void triple + CHECKs, `no_prices_confirmed` CHECK per R15, append-only guards) + migration. `tender_storage.py` (validator sharing the DC type/magic table, size per R16, Program-based path, pinned-bucket signer). `permissions.py`: upload helper (R3) and view helper (R5/R6). Migration-chain test. |
| TD-B2 | Write paths | Upload (atomic, validate-all-then-upload, discard-unrecorded on failure, `?key=` duplicate guard), supersede (row lock, R8), void (R13). Manage list where R17 puts it. Log lines per R7. |
| TD-B3 | Read paths | "Tender documents" card on `project_overview` (closed by default, context builder, OPEX + `program_id` only, filtered by class), open view (signed per click, 300 s, logged per R7, 404 like `refuse_site_view_404` for anyone outside the audience or a voided document outside management). |

---

## Appendix — remaining raw query output (Q7–Q9, Q11)

Each of these sessions also opened with `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;` (`default_transaction_read_only = on`).

```
### Q7 per Program: distinct Site Engineers holding a task on a live site (the "Site team" audience)
SELECT g.id, g.short_tender_code,
       COUNT(DISTINCT t.assigned_to_id) FILTER (WHERE up.role = 'Site Engineer') AS se_holding_tasks
FROM projects_program g
LEFT JOIN projects_project p ON p.program_id = g.id AND NOT p.is_deleted
LEFT JOIN projects_projectphase ph ON ph.project_id = p.id
LEFT JOIN projects_task t ON t.phase_id = ph.id
LEFT JOIN projects_userprofile up ON up.id = t.assigned_to_id
GROUP BY g.id ORDER BY g.id;
id | short_tender_code | se_holding_tasks
1 | TESTTENDER26 | 0
2 | MPUVL | 0
3 |  | 0
4 | IPGCLC | 0
5 | IPGCLG | 9
6 | RUDEH | 7

### Q8 live OPEX sites by status
SELECT status, COUNT(*) FROM projects_project
WHERE project_type = 'OPEX' AND NOT is_deleted GROUP BY status ORDER BY status;
status | count
Active | 115
Draft | 98

### Q9 ProjectDocument usage by project_type and uploader role (live rows)
SELECT p.project_type, COALESCE(up.role,'(none)') AS uploader_role, COUNT(*) AS live_docs
FROM projects_projectdocument d
JOIN projects_project p ON p.id = d.project_id
LEFT JOIN projects_userprofile up ON up.id = d.uploaded_by_id
WHERE NOT d.is_deleted GROUP BY 1,2 ORDER BY 1,2;
project_type | uploader_role | live_docs
CAPEX | PM | 1
OPEX | PM | 55
Residential | PM | 1

### Q11 OPEX ProjectDocument repetition (file names not printed)
SELECT p.program_id, d.file_type, COUNT(*) AS rows, COUNT(DISTINCT d.project_id) AS sites, COUNT(DISTINCT d.file_name) AS distinct_names, MAX(d.file_size_kb) AS max_kb
FROM projects_projectdocument d JOIN projects_project p ON p.id=d.project_id
WHERE NOT d.is_deleted AND p.project_type='OPEX' GROUP BY 1,2 ORDER BY 1,2;
['program_id', 'file_type', 'rows', 'sites', 'distinct_names', 'max_kb']
(5, 'Document', 54, 20, 16, 2899)
(6, 'Document', 1, 1, 1, 594)
SELECT COUNT(*) AS names_on_gt1_site FROM (SELECT d.file_name FROM projects_projectdocument d JOIN projects_project p ON p.id=d.project_id
WHERE NOT d.is_deleted AND p.project_type='OPEX' GROUP BY d.file_name HAVING COUNT(DISTINCT d.project_id)>1) x;
(2,)
```

---

## Secondary findings

Appended to `SECONDARY_FINDINGS.md` under "From TD-A".
