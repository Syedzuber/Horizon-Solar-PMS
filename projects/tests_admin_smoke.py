"""Smoke coverage for every page `admin.site` serves (B11).

WHY THIS FILE EXISTS
--------------------
`DocumentInline.fields` named three columns — `doc_type`, `title`, `file` — that
`ProjectDocument` has never had. Both admin project pages raised `FieldError` and
returned 500, for an unknown length of time, with every automated signal green.

`python manage.py check` reported no issues and *structurally cannot* report this
one. `BaseModelAdminChecks._check_field_spec_item` looks the name up on the model
and, on `FieldDoesNotExist`, returns no error — because a `ModelAdmin.fields` entry
is allowed to name a field the *form* contributes rather than the model. That
permission is deliberate and correct; the cost is that a typo and a form-contributed
field are indistinguishable to the checks framework. Only building the form tells
them apart, and building the form is what a request does.

So the gap is not "someone forgot a check". It is that admin field specs are
validated at request time and nothing in the suite was issuing a request. This file
issues them.

WHAT IT ASSERTS
---------------
For every model in `admin.site._registry` — the registry itself, never a tuple
copied from it, so a registration added in some later session is covered without
anyone remembering this file exists — the changelist and the add form return 200.

No fixtures. A changelist renders with zero rows and an add form renders with no
object, so the test needs no setup at all; that is deliberate, because setup is the
part of a smoke test that rots. The one consequence is that this file covers the
add form, not the change form: a `fields` entry that resolves on add and fails on
change would slip past. Nothing in `projects/admin.py` builds fields conditionally
on `obj` today, and building one valid instance per registered model — most of them
behind required foreign keys — costs far more than that gap is worth.

`NotificationLogAdmin` denies add outright, so its add page is a 403 by design and
never constructs a form. For any such admin the test resolves `get_form()` and every
inline's `get_formset()` directly, so the field spec is still exercised. Otherwise
the one admin that forbids adding would be the one admin nothing validates.
"""

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse


class EveryRegisteredAdminPageLoadsTests(TestCase):
    """The standing guard over the admin's field specs.

    Failures name the model and the admin class, so a future breakage identifies
    itself without anyone having to bisect the registry by hand.
    """

    @classmethod
    def setUpTestData(cls):
        cls.superuser = User.objects.create_superuser(
            'b11_admin_smoke', 'b11@example.com', 'pw')
        # `solarpms.middleware.AdminAccessMiddleware` gates /admin/ on
        # UserProfile.role == 'Admin', NOT on is_staff — a superuser whose profile
        # carries any other role is redirected to a dashboard and every assertion
        # below would read 302 instead of the page. The profile itself is created by
        # a post_save signal on User; only the role has to be set.
        cls.superuser.profile.role = 'Admin'
        cls.superuser.profile.save(update_fields=['role'])

    def setUp(self):
        # ALLOWED_HOSTS has no 'testserver', so the default Client 400s.
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.superuser)
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.get('/')
        request.user = self.superuser
        return request

    def _url(self, model, page):
        meta = model._meta
        return reverse(f'admin:{meta.app_label}_{meta.model_name}_{page}')

    def test_every_registered_model_serves_its_changelist(self):
        registry = django_admin.site._registry
        self.assertTrue(registry, 'admin.site is empty — this test asserted nothing')

        for model, model_admin in registry.items():
            with self.subTest(model=model.__name__):
                response = self.client.get(self._url(model, 'changelist'))
                self.assertEqual(
                    response.status_code, 200,
                    f"{type(model_admin).__name__}'s changelist for "
                    f"{model.__name__} returned {response.status_code}, not 200. "
                    f"A changelist renders fine with no rows, so this is a "
                    f"configuration error — most likely list_display, "
                    f"list_filter or get_queryset.")

    def test_every_registered_model_serves_its_add_form(self):
        """The one that catches a bad `fields`/`fieldsets` entry.

        `manage.py check` cannot: an unknown name is assumed to be a
        form-contributed field. Building the form is the only thing that tells a
        typo from a real one.
        """
        registry = django_admin.site._registry
        self.assertTrue(registry, 'admin.site is empty — this test asserted nothing')

        checked, add_denied = [], []
        for model, model_admin in registry.items():
            with self.subTest(model=model.__name__):
                if not model_admin.has_add_permission(self._request()):
                    # Denied before a form is ever built, so the 403 proves nothing
                    # about the field spec. Resolve it directly instead.
                    add_denied.append(model.__name__)
                    model_admin.get_form(self._request(), None)
                    for inline in model_admin.get_inline_instances(self._request(), None):
                        inline.get_formset(self._request(), None)
                    continue

                checked.append(model.__name__)
                response = self.client.get(self._url(model, 'add'))
                self.assertEqual(
                    response.status_code, 200,
                    f"{type(model_admin).__name__}'s add form for "
                    f"{model.__name__} returned {response.status_code}, not 200. "
                    f"If this is a FieldError, some entry in fields/fieldsets — on "
                    f"the admin or on one of its inlines — names a column the model "
                    f"does not have. manage.py check does not catch that; see "
                    f"docs/execution-model.md.")

        self.assertTrue(
            checked,
            'no registered admin allowed an add form, so this test asserted '
            'nothing — the registry lookup above is broken, not the codebase safe')
        # An anchor: ProjectAdmin is the admin B11 fixed and the one carrying the
        # inlines. If it ever leaves the registry, this test stops covering the
        # thing it was written for, and should say so out loud.
        self.assertIn('Project', checked + add_denied,
                      'Project is no longer registered in the admin')
