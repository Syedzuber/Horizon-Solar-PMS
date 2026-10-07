"""
G42 — a checklist version, or an item, that holds answers cannot be deleted.

WHY THIS FILE EXISTS
--------------------
ChecklistItemCompletion.item is SET_NULL. Deleting a version (or an item) that had been
answered nulled those answers' item: the rows survived with their R-8 snapshot, but a
null-item answer pins nothing (closeout 1a) and _checklist_context() reads
item__in=items, so the task fell back to the active version and its answers vanished
from the page — the 1a defect by another route.

THE RULE PINNED HERE
    Any ChecklistItemCompletion on a version or item → it cannot be deleted, by the
    portal, by the Django admin change/delete view, by the inline, or by the bulk
    "delete selected" action. The refusal names how many answers on how many tasks.
    No answers                                       → deletes as before.
    Archiving                                        → still the way to retire a version;
                                                       answered tasks keep showing it.

FIXTURE. Reuses closeout 1a's VersionPinFixture: two OPEX sites really activated, v1 of
PIN-CIVIL published the R-7 way, and site A's Civil Work task answering three of v1's
five items THROUGH THE REAL VIEW. A draft with an answered item cannot arise through the
product (drafts are never served), so the tests that need one write the completion
directly — that is exactly the "behind the view's back" case the item guard is for.

Run with:
    python manage.py test projects.tests_checklist_delete_guard --settings=solarpms.test_settings
"""
from django.contrib.admin.sites import site as admin_site
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory
from django.urls import reverse

from .models import (
    Checklist, ChecklistHasAnswers, ChecklistItem, ChecklistItemCompletion,
    checklist_answer_counts,
)
from .tests_checklist_task_link import _client_for, _profile
from .tests_checklist_version_pin import V1_LABELS, VersionPinFixture, _publish
from .views import _checklist_for_task


class DeleteGuardFixture(VersionPinFixture):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin_profile = _profile('guard_admin', 'Admin')
        superuser = _profile('guard_super', 'Admin')
        superuser.user.is_staff = True
        superuser.user.is_superuser = True
        superuser.user.save()
        cls.superuser = superuser

        # A draft of an unrelated family with no answers: the "deletes as today" case.
        cls.draft, cls.draft_items = _publish('GUARD-DRAFT', 'Guard Draft', 1,
                                              ['Draft line one', 'Draft line two'],
                                              activate=False)

    # -- helpers --------------------------------------------------------------

    def _answered_draft(self):
        """A draft whose first item holds one answer, written behind the view's back."""
        draft, items = _publish('GUARD-ANSWERED', 'Answered Draft', 1,
                                ['Answered line', 'Unanswered line'], activate=False)
        ChecklistItemCompletion.objects.create(
            item=items[0], task=self.fresh, is_checked=True, answer='yes',
            item_text_snapshot=items[0].label)
        return draft, items

    def _v1_rows(self):
        return (Checklist.objects.filter(pk=self.v1.pk).count(),
                ChecklistItem.objects.filter(checklist=self.v1).count(),
                ChecklistItemCompletion.objects.filter(
                    item__checklist=self.v1, task=self.task).count())

    def _admin_request(self):
        request = RequestFactory().post('/')
        request.user = self.superuser.user
        request.session = 'session'
        request._messages = FallbackStorage(request)
        return request

    def _flash(self, response):
        return [str(m) for m in response.wsgi_request._messages]


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------

class PredicateTests(DeleteGuardFixture):

    def test_counts_answers_and_distinct_tasks_on_version_and_item(self):
        self.assertEqual(checklist_answer_counts(self.v1), (3, 1))
        self.assertEqual(checklist_answer_counts(self.v1_items[0]), (1, 1))
        self.assertEqual(checklist_answer_counts(self.v1_items[4]), (0, 0))
        self.assertEqual(checklist_answer_counts(self.draft), (0, 0))

    def test_model_delete_refuses_an_answered_version(self):
        with self.assertRaises(ChecklistHasAnswers) as caught:
            self.v1.delete()
        self.assertIn('3 answers on 1 task', str(caught.exception))
        self.assertEqual(self._v1_rows(), (1, 5, 3))


# ---------------------------------------------------------------------------
# 1, 2 — portal version delete
# ---------------------------------------------------------------------------

class PortalVersionDeleteTests(DeleteGuardFixture):

    def test_1_version_with_answers_is_refused_and_rows_intact(self):
        response = _client_for(self.admin_profile).post(
            reverse('admin_checklist_delete', args=[self.v1.pk]))
        self.assertRedirects(response, reverse('admin_checklist_edit', args=[self.v1.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(self._v1_rows(), (1, 5, 3))
        self.assertFalse(ChecklistItemCompletion.objects.filter(item__isnull=True).exists())
        flash = self._flash(response)
        self.assertEqual(len(flash), 1)
        self.assertIn('3 answers on 1 task', flash[0])
        self.assertIn('Archive it instead', flash[0])

    def test_1b_archived_version_with_answers_is_refused_too(self):
        Checklist.objects.filter(pk=self.v1.pk).update(status=Checklist.ARCHIVED)
        _client_for(self.admin_profile).post(
            reverse('admin_checklist_delete', args=[self.v1.pk]))
        self.assertEqual(self._v1_rows(), (1, 5, 3))

    def test_2_draft_with_no_answers_is_deleted_as_today(self):
        response = _client_for(self.admin_profile).post(
            reverse('admin_checklist_delete', args=[self.draft.pk]))
        self.assertRedirects(response, reverse('admin_checklists'),
                             fetch_redirect_response=False)
        self.assertFalse(Checklist.objects.filter(pk=self.draft.pk).exists())
        self.assertFalse(ChecklistItem.objects.filter(
            pk__in=[i.pk for i in self.draft_items]).exists())


# ---------------------------------------------------------------------------
# 3, 4 — Django admin
# ---------------------------------------------------------------------------

class DjangoAdminTests(DeleteGuardFixture):

    def test_3_no_delete_permission_on_a_version_with_answers(self):
        model_admin = admin_site.get_model_admin(Checklist)
        request = self._admin_request()
        self.assertFalse(model_admin.has_delete_permission(request, self.v1))
        self.assertTrue(model_admin.has_delete_permission(request, self.draft))

        client = _client_for(self.superuser)
        response = client.post(reverse('admin:projects_checklist_delete', args=[self.v1.pk]),
                               {'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._v1_rows(), (1, 5, 3))

    def test_3b_answered_draft_change_view_offers_no_delete(self):
        """Proves the refusal is about answers, not status: a draft is otherwise
        editable and deletable in the admin."""
        answered, _items = self._answered_draft()
        response = _client_for(self.superuser).get(
            reverse('admin:projects_checklist_change', args=[answered.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['has_delete_permission'])
        self.assertNotContains(response,
                               reverse('admin:projects_checklist_delete', args=[answered.pk]))

    def test_4_bulk_delete_including_an_answered_version_deletes_nothing(self):
        client = _client_for(self.superuser)
        changelist = reverse('admin:projects_checklist_changelist')
        selected = [self.v1.pk, self.draft.pk]

        confirm = client.post(changelist, {'action': 'delete_selected',
                                           '_selected_action': selected})
        self.assertEqual(confirm.status_code, 200)
        self.assertContains(confirm, '3 answers on 1 task')

        response = client.post(changelist, {'action': 'delete_selected',
                                            '_selected_action': selected, 'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._v1_rows(), (1, 5, 3))
        self.assertTrue(Checklist.objects.filter(pk=self.draft.pk).exists())

        # Unticking the answered version, the rest deletes as today.
        client.post(changelist, {'action': 'delete_selected',
                                 '_selected_action': [self.draft.pk], 'post': 'yes'})
        self.assertFalse(Checklist.objects.filter(pk=self.draft.pk).exists())

    def test_4b_delete_queryset_backstop_spares_the_answered_version(self):
        """QuerySet.delete() never calls Checklist.delete(); delete_queryset() is the
        layer that covers it, whatever caller reaches it."""
        model_admin = admin_site.get_model_admin(Checklist)
        request = self._admin_request()
        model_admin.delete_queryset(
            request, Checklist.objects.filter(pk__in=[self.v1.pk, self.draft.pk]))
        self.assertEqual(self._v1_rows(), (1, 5, 3))
        self.assertFalse(Checklist.objects.filter(pk=self.draft.pk).exists())
        self.assertIn('3 answers on 1 task', ' '.join(str(m) for m in request._messages))


# ---------------------------------------------------------------------------
# 5 — items
# ---------------------------------------------------------------------------

class ItemDeleteTests(DeleteGuardFixture):

    def test_5_portal_refuses_answered_item_and_deletes_unanswered_one(self):
        draft, items = self._answered_draft()
        client = _client_for(self.admin_profile)

        response = client.post(reverse('admin_checklist_item_delete',
                                       args=[draft.pk, items[0].pk]))
        self.assertTrue(ChecklistItem.objects.filter(pk=items[0].pk).exists())
        self.assertEqual(ChecklistItemCompletion.objects.get(task=self.fresh).item_id,
                         items[0].pk)
        self.assertIn('1 answer on 1 task', self._flash(response)[0])

        client.post(reverse('admin_checklist_item_delete', args=[draft.pk, items[1].pk]))
        self.assertFalse(ChecklistItem.objects.filter(pk=items[1].pk).exists())

    def test_5b_model_delete_refuses_answered_item_on_a_draft(self):
        _draft, items = self._answered_draft()
        with self.assertRaises(ChecklistHasAnswers):
            items[0].delete()
        self.assertTrue(ChecklistItem.objects.filter(pk=items[0].pk).exists())

    def test_5c_django_admin_inline_refuses_answered_item_as_a_form_error(self):
        draft, items = self._answered_draft()
        url = reverse('admin:projects_checklist_change', args=[draft.pk])
        data = {
            'code': draft.code, 'name': draft.name, 'version_no': draft.version_no,
            'effective_from': '', 'created_by': '',
            'items-TOTAL_FORMS': '2', 'items-INITIAL_FORMS': '2',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
            'task_links-TOTAL_FORMS': '0', 'task_links-INITIAL_FORMS': '0',
            'task_links-MIN_NUM_FORMS': '0', 'task_links-MAX_NUM_FORMS': '1000',
        }
        for n, item in enumerate(items):
            data.update({f'items-{n}-id': item.pk, f'items-{n}-checklist': draft.pk,
                         f'items-{n}-order': item.order, f'items-{n}-label': item.label})
        data['items-0-DELETE'] = 'on'

        response = _client_for(self.superuser).post(url, data)
        self.assertEqual(response.status_code, 200)       # re-rendered with the error
        self.assertContains(response, '1 answer on 1 task')
        self.assertTrue(ChecklistItem.objects.filter(pk=items[0].pk).exists())

        # The unanswered line deletes through the same form.
        data.pop('items-0-DELETE')
        data['items-1-DELETE'] = 'on'
        response = _client_for(self.superuser).post(url, data)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(ChecklistItem.objects.filter(pk=items[1].pk).exists())
        self.assertTrue(ChecklistItem.objects.filter(pk=items[0].pk).exists())


# ---------------------------------------------------------------------------
# 6 — archiving stays the way to retire a version
# ---------------------------------------------------------------------------

class ArchiveStillWorksTests(DeleteGuardFixture):

    def test_6_archive_with_answers_works_and_answers_still_show(self):
        client = _client_for(self.admin_profile)
        response = client.post(reverse('admin_checklist_update', args=[self.v1.pk]),
                               {'action': 'archive'})
        self.assertEqual(response.status_code, 302)
        self.v1.refresh_from_db()
        self.assertEqual(self.v1.status, Checklist.ARCHIVED)

        # The 1a pin holds: the answered task is still served v1 and shows its answers.
        self.assertEqual(_checklist_for_task(self.task, self.site), self.v1)
        ctx = self._context(self.task)
        self.assertEqual(sum(1 for r in ctx['checklist_rows'] if r['completion']), 3)
        content = self._page(self.task).content.decode()
        for label in V1_LABELS[:3]:
            self.assertIn(label, content)

        # And an archived version with answers is still not deletable.
        client.post(reverse('admin_checklist_delete', args=[self.v1.pk]))
        self.assertEqual(self._v1_rows(), (1, 5, 3))
