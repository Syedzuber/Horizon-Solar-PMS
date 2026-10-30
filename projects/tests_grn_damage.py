"""DG-B — damaged delivery at GRN: a photo per damaged line, the challan's damage Issue,
and a refused GRN that keeps what was typed.

WHAT IS PINNED
--------------
  * R1/R2: a line that ends a GRN with damage has >= 1 live photo and <= 5; a line that
    already has one needs no new photo; confirm_grn and override_grn alike.
  * QE: only damage RECORDED NOW (number changed, or photos attached) needs the Issue,
    asks for a description when none is open, and appears in the Comment.
  * QF: photos on an undamaged or skipped line are refused.
  * R4/R5/QJ: one damage Issue per challan — reused with a Comment while open, a new one
    (title, High, Open, site PM or unassigned) after it is resolved.
  * R6: the assignee and managers as create_delivery_issue tells them, plus every
    active SCM user in-app and email, actor excluded, nobody twice.
  * R7: damage set to 0 adds a Comment and leaves photos and Issue alone.
  * R8: refusals before any upload; a storage failure part way and a database failure
    inside the transaction both discard every upload; QK's race refuses cleanly.
  * R11: the draft — typed values, description and on-behalf reason come back once.
  * R3/QH/QI: the open view (live: project viewers; withdrawn: SCM/Admin), withdraw
    (SCM/Admin, reason >= 10, object kept), opens hidden from Recent activity.
  * the model's three CHECKs, append-only, PROTECT; query cost of both pages;
    the QA/QC and punch forms still carry what the shared photo script reads.

The challan row lock (grn_damage.lock_challan) is a no-op on SQLite and is proved by hand
on Postgres, not here.

Run with:
    python manage.py test projects.tests_grn_damage --settings=solarpms.test_settings
"""
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import grn_damage
from .models import (
    ActivityLog, AppendOnlyViolation, Comment, DCLineDamagePhoto, DCLineItem,
    DeliveryChallan, Issue, Notification, NotificationLog, Project, ProjectPhase, Task,
    UserProfile,
)
from .notify_registry import NOTIFICATION_EVENTS

BUCKET = 'Horizon-PMS-Delivery-test'
SIGNED = 'https://signed.example/damage?token=t'

JPG = b'\xff\xd8\xff\xe0\x00\x10JFIF damage'
PNG = b'\x89PNG\r\n\x1a\n damage'
PDF = b'%PDF-1.4 not a photo\n'


def _profile(username, role):
    user = User.objects.create_user(username=username, password='x',
                                    first_name=username.title(), last_name='Test',
                                    email=f'{username}@example.com')
    profile = user.profile          # auto-created by the post_save signal
    profile.role = role
    profile.save(update_fields=['role'])
    return profile


def _name(profile):
    return profile.user.get_full_name() or profile.user.username


def jpg(name='damage.jpg'):
    return SimpleUploadedFile(name, JPG, content_type='image/jpeg')


def _project(project_id, pm=None):
    return Project.objects.create(
        project_id=project_id, customer_name=f'{project_id} Residence',
        customer_phone='9876543210', site_address='1 Sun Road', city='Delhi',
        project_type='Residential', dc_capacity_kw=Decimal('5.00'), status='Active',
        assigned_pm=pm, target_commissioning_date=date.today() + timedelta(days=90))


@override_settings(SUPABASE_DELIVERY_BUCKET=BUCKET)
class GrnDamageBase(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.scm = _profile('gd_scm', 'SCM')
        cls.scm2 = _profile('gd_scm2', 'SCM')
        cls.scm_coord = _profile('gd_scmco', 'SCM')       # SCM AND a coordinator here
        cls.admin = _profile('gd_admin', 'Admin')
        cls.finance = _profile('gd_fin', 'Finance')
        cls.pm = _profile('gd_pm', 'PM')
        cls.coord = _profile('gd_coord', 'Project Coordinator')
        cls.se = _profile('gd_se', 'Site Engineer')
        cls.se_elsewhere = _profile('gd_se2', 'Site Engineer')
        cls.site = _project('GD-SITE', pm=cls.pm)
        cls.site.coordinators.add(cls.coord, cls.scm_coord)
        cls.other = _project('GD-OTHER')
        for project, engineer in ((cls.site, cls.se), (cls.other, cls.se_elsewhere)):
            phase = ProjectPhase.objects.create(project=project, phase_name='Install',
                                                phase_order=1)
            Task.objects.create(phase=phase, task_name='Receive material', task_order=1,
                                assigned_role=Task.SITE_ENGINEER, assigned_to=engineer)
        cls.dc = DeliveryChallan.objects.create(project=cls.site, dc_number='GD-1',
                                                dc_date=date.today(), created_by=cls.scm)
        cls.panels = DCLineItem.objects.create(
            challan=cls.dc, boq_category='Solar Modules', item_description='Panel 540Wp',
            ordered_quantity=Decimal('10'), unit='Nos')
        cls.inverter = DCLineItem.objects.create(
            challan=cls.dc, boq_category='Inverter', item_description='Inverter 5kW',
            ordered_quantity=Decimal('2'), unit='Nos')

    def setUp(self):
        self.storage = mock.MagicMock()
        self.bucket_api = self.storage.storage.from_.return_value
        self.bucket_api.create_signed_url.return_value = {'signedURL': SIGNED}
        for target in ('projects.delivery_storage._client', 'projects.design_storage._client'):
            patcher = mock.patch(target, return_value=self.storage)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Belt and braces: the master switches are off by default, so no send leaves the
        # process; any HTTP request at all fails the test.
        http = mock.patch.object(requests.sessions.Session, 'request',
                                 side_effect=AssertionError('unexpected HTTP request'))
        http.start()
        self.addCleanup(http.stop)
        self.client = Client(SERVER_NAME='localhost')

    # -- helpers ---------------------------------------------------------------

    def login(self, profile):
        self.client.force_login(profile.user)

    def _payload(self, lines, description='', reason=''):
        """lines: {item: (received, damaged, [files])}."""
        data = {}
        for item, (received, damaged, files) in lines.items():
            data[f'received_qty_{item.pk}'] = received
            data[f'damaged_qty_{item.pk}'] = damaged
            data[f'grn_notes_{item.pk}'] = f'note {item.pk}'
            if files:
                data[f'damage_photos_{item.pk}'] = files
        if description:
            data['damage_description'] = description
        if reason:
            data['grn_on_behalf_reason'] = reason
        return data

    def confirm(self, lines, description='', actor=None, challan=None, project=None):
        self.login(actor or self.se)
        challan, project = challan or self.dc, project or self.site
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('confirm_grn', args=[project.project_id, challan.pk]),
                                    self._payload(lines, description))

    def override(self, lines, description='', reason='', actor=None):
        self.login(actor or self.scm)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse('override_grn', args=[self.site.project_id, self.dc.pk]),
                                    self._payload(lines, description, reason))

    def messages_of(self, response):
        return [str(m) for m in response.wsgi_request._messages]

    def uploads(self):
        return [c.kwargs['path'] for c in self.bucket_api.upload.call_args_list]

    def removes(self):
        return self.bucket_api.remove.call_args_list

    @classmethod
    def photo(cls, line, issue=None, withdrawn_by=None, name='p.jpg', uploader=None):
        row = DCLineDamagePhoto.objects.create(
            line=line, issue=issue, uploaded_by=uploader or cls.se, original_name=name,
            bucket=BUCKET, path=f'GD-SITE/grn-damage/{line.pk}-{name}-{timezone.now().timestamp()}',
            size=2048, mime='image/jpeg')
        if withdrawn_by is not None:
            DCLineDamagePhoto.objects.filter(pk=row.pk).update(
                withdrawn_at=timezone.now(), withdrawn_by=withdrawn_by,
                withdrawn_reason='Wrong photo, not this crate')
            row.refresh_from_db()
        return row

    def damage_issue(self, status=Issue.OPEN, photo_line=None):
        """An Issue on the challan with a photo linked: the challan's damage Issue."""
        issue = Issue.objects.create(project=self.site, delivery_challan=self.dc,
                                     title='Damaged delivery — DC GD-1', severity=Issue.HIGH,
                                     status=status, raised_by=self.se, assigned_to=self.pm,
                                     description='Two panels cracked in transit')
        self.photo(photo_line or self.panels, issue=issue)
        return issue

    def refresh(self, *items):
        for item in items:
            item.refresh_from_db()


# ---------------------------------------------------------------------------
# Refusals — nothing written, nothing uploaded, the draft kept
# ---------------------------------------------------------------------------

class RefusalTests(GrnDamageBase):

    def assert_nothing_written(self):
        self.refresh(self.panels, self.inverter)
        self.assertIsNone(self.panels.received_quantity)
        self.assertEqual(self.panels.damaged_quantity, 0)
        self.assertFalse(Issue.objects.exists())
        self.assertFalse(DCLineDamagePhoto.objects.exists())
        self.assertEqual(self.uploads(), [])

    def test_damage_without_a_photo_is_refused_and_the_draft_is_kept(self):
        response = self.confirm({self.panels: ('8', '2', []), self.inverter: ('2', '0', [])},
                                description='Two panels cracked across the glass')
        self.assertEqual(response.status_code, 302)
        self.assertIn('Solar Modules — Panel 540Wp: 2 damaged — attach at least one photo '
                      'of the damage.', self.messages_of(response))
        self.assertIn('The GRN was not recorded.', self.messages_of(response))
        self.assert_nothing_written()

        draft = self.client.session['grn_draft']
        self.assertEqual(draft['where'], f'grn:{self.dc.pk}:confirm')
        self.assertEqual(draft['lines'][str(self.panels.pk)],
                         {'received': '8', 'damaged': '2', 'notes': f'note {self.panels.pk}'})
        self.assertEqual(draft['description'], 'Two panels cracked across the glass')

        page = self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, self.dc.pk])).content.decode()
        self.assertIn('photos cannot be kept from a refused submission', ' '.join(page.split()))
        self.assertIn(f'name="received_qty_{self.panels.pk}"', page)
        self.assertRegex(page, rf'name="damaged_qty_{self.panels.pk}"[^>]*value="2"')
        self.assertIn('Two panels cracked across the glass</textarea>', page)
        self.assertNotIn('grn_draft', self.client.session, 'shown once, then gone')

    def test_damage_with_a_photo_and_no_description_is_refused(self):
        response = self.confirm({self.panels: ('10', '2', [jpg()])})
        self.assertTrue(any(m.startswith('Describe the damage (at least 10 characters)')
                            for m in self.messages_of(response)))
        self.assert_nothing_written()

    def test_a_short_description_is_refused(self):
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='cracked')
        self.assert_nothing_written()

    def test_scm_refusal_keeps_the_on_behalf_reason(self):
        response = self.override({self.panels: ('10', '1', [])},
                                 reason='Site engineer unreachable today')
        self.assertIn('The GRN was not changed.', self.messages_of(response))
        self.assert_nothing_written()
        draft = self.client.session['grn_draft']
        self.assertEqual(draft['where'], f'grn:{self.dc.pk}:override')
        self.assertEqual(draft['on_behalf_reason'], 'Site engineer unreachable today')
        page = self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, self.dc.pk])).content.decode()
        self.assertIn('value="Site engineer unreachable today"', page)
        self.assertIn("getElementById('editGrnModal')", page, 'the modal reopens')

    def test_the_on_behalf_refusal_keeps_the_typed_values_too(self):
        response = self.override({self.panels: ('7', '0', [])})
        self.assertIn('Please state why you are recording this receipt',
                      self.messages_of(response)[0])
        self.assertEqual(self.client.session['grn_draft']['lines'][str(self.panels.pk)]['received'], '7')

    def test_a_quantity_error_keeps_the_typed_values(self):
        self.confirm({self.panels: ('-5', '0', [])})
        self.assertEqual(self.client.session['grn_draft']['lines'][str(self.panels.pk)]['received'], '-5')
        self.assert_nothing_written()

    def test_photos_on_a_line_with_no_damage_are_refused(self):
        response = self.confirm({self.panels: ('10', '0', [jpg()])})
        self.assertIn('Solar Modules — Panel 540Wp: photos were attached, but no units are '
                      'marked damaged. Enter the damaged quantity, or remove the photos.',
                      self.messages_of(response))
        self.assert_nothing_written()

    def test_photos_on_a_skipped_line_are_refused(self):
        response = self.confirm({self.panels: ('', '', [jpg()]), self.inverter: ('2', '0', [])})
        self.assertTrue(any('no received quantity was entered' in m
                            for m in self.messages_of(response)))
        self.refresh(self.inverter)
        self.assertIsNone(self.inverter.received_quantity)
        self.assertEqual(self.uploads(), [])

    def test_more_than_five_live_photos_on_a_line_are_refused(self):
        issue = self.damage_issue()                       # one live photo on panels
        for n in range(3):
            self.photo(self.panels, issue=issue, name=f'{n}.jpg')
        self.photo(self.panels, issue=issue, withdrawn_by=self.scm, name='gone.jpg')
        response = self.confirm({self.panels: ('10', '3', [jpg('a.jpg'), jpg('b.jpg')])})
        self.assertIn('Solar Modules — Panel 540Wp: at most 5 photos per line — 4 already on '
                      'record and 2 attached.', self.messages_of(response))
        self.assertEqual(self.uploads(), [])
        # One more is still within the limit: the withdrawn photo does not count.
        response = self.confirm({self.panels: ('10', '3', [jpg('a.jpg')])})
        self.assertEqual(DCLineDamagePhoto.objects.filter(line=self.panels,
                                                          withdrawn_at__isnull=True).count(), 5)

    def test_bad_files_are_refused_with_nothing_uploaded(self):
        cases = {
            'wrong type': SimpleUploadedFile('d.gif', b'GIF89a', content_type='image/gif'),
            'a pdf': SimpleUploadedFile('d.pdf', PDF, content_type='application/pdf'),
            'fake extension': SimpleUploadedFile('d.jpg', PDF, content_type='image/jpeg'),
            'empty': SimpleUploadedFile('d.jpg', b'', content_type='image/jpeg'),
        }
        for label, upload in cases.items():
            with self.subTest(label):
                response = self.confirm({self.panels: ('10', '1', [upload])},
                                        description='Panel frame bent at a corner')
                self.assertIn('The GRN was not recorded.', self.messages_of(response))
                self.assert_nothing_written()

    def test_a_photo_over_20_mb_is_refused_with_nothing_uploaded(self):
        with mock.patch('projects.delivery_storage.DC_DOCUMENT_MAX_BYTES', 10):
            response = self.confirm({self.panels: ('10', '1', [jpg()])},
                                    description='Panel frame bent at a corner')
        self.assertTrue(any('the limit is 0 MB' in m or 'MB — the limit' in m
                            for m in self.messages_of(response)))
        self.assert_nothing_written()

    def test_storage_off_refuses_a_photo(self):
        with override_settings(SUPABASE_DELIVERY_BUCKET=''):
            response = self.confirm({self.panels: ('10', '1', [jpg()])},
                                    description='Panel frame bent at a corner')
        self.assertTrue(any(m.startswith('Damage photos cannot be stored right now')
                            for m in self.messages_of(response)))
        self.assert_nothing_written()


# ---------------------------------------------------------------------------
# The damage Issue
# ---------------------------------------------------------------------------

class DamageIssueTests(GrnDamageBase):

    def test_damage_with_photo_and_description_raises_the_damage_issue(self):
        response = self.confirm({self.panels: ('10', '2', [jpg()]), self.inverter: ('2', '0', [])},
                                description='Two panels cracked across the glass')
        self.refresh(self.panels, self.inverter)
        self.assertEqual((self.panels.received_quantity, self.panels.damaged_quantity),
                         (Decimal('10'), 2))
        self.assertEqual(self.panels.grn_confirmed_by, self.se)
        self.dc.refresh_from_db()
        self.assertEqual(self.dc.status, DeliveryChallan.PARTIALLY_RECEIVED)

        issue = Issue.objects.get()
        self.assertEqual(
            (issue.title, issue.severity, issue.status, issue.assigned_to, issue.raised_by,
             issue.delivery_challan, issue.description),
            ('Damaged delivery — DC GD-1', Issue.HIGH, Issue.OPEN, self.pm, self.se, self.dc,
             'Two panels cracked across the glass'))
        photo = DCLineDamagePhoto.objects.get()
        self.assertEqual((photo.line, photo.issue, photo.uploaded_by, photo.mime, photo.bucket),
                         (self.panels, issue, self.se, 'image/jpeg', BUCKET))
        self.assertEqual(self.uploads(), [photo.path])
        self.assertIn('/grn-damage/', photo.path)
        self.assertTrue(ActivityLog.objects.filter(action_code='issue_created',
                                                   entity_id=issue.pk).exists())
        self.assertFalse(Comment.objects.exists())
        self.assertIn('GRN confirmed. DC status: Partially Received. Damage issue raised: '
                      '"Damaged delivery — DC GD-1".', self.messages_of(response))

    def test_new_issue_notifications(self):
        self.confirm({self.panels: ('10', '2', [jpg()])},
                     description='Two panels cracked across the glass')
        issue = Issue.objects.get()
        link = f'/issues/{issue.pk}/'

        def in_app(profile):
            return list(Notification.objects.filter(recipient=profile))

        def emailed(profile):
            return NotificationLog.objects.filter(recipient=profile, channel='email').exists()

        # The assignee, as create_delivery_issue tells one: in-app, email, WhatsApp.
        self.assertEqual(len(in_app(self.pm)), 1)
        self.assertTrue(emailed(self.pm))
        self.assertTrue(NotificationLog.objects.filter(recipient=self.pm, channel='whatsapp').exists())
        # The other manager: the in-app nudge only.
        nudge = in_app(self.coord)
        self.assertEqual([n.message for n in nudge],
                         [f'Issue "{issue.title}" raised on GD-SITE — GD-SITE Residence.'])
        self.assertFalse(emailed(self.coord))
        # Every active SCM user, in-app and email — once, the coordinator-SCM included.
        for scm in (self.scm, self.scm2, self.scm_coord):
            with self.subTest(scm=scm.user.username):
                notes = in_app(scm)
                self.assertEqual(len(notes), 1)
                self.assertTrue(notes[0].message.startswith('A new issue has been raised on project'))
                self.assertEqual(notes[0].link, link)
                self.assertTrue(emailed(scm))
        # The actor hears nothing.
        self.assertEqual(in_app(self.se), [])
        self.assertFalse(NotificationLog.objects.filter(recipient=self.se).exists())

    def test_scm_raising_it_is_not_told_but_the_other_scm_users_are(self):
        self.override({self.panels: ('10', '1', [jpg()])}, description='Inverter box dented',
                      reason='Recorded at the warehouse gate')
        self.assertFalse(Notification.objects.filter(recipient=self.scm).exists())
        self.assertEqual(Notification.objects.filter(recipient=self.scm2).count(), 1)

    def test_an_inactive_scm_user_is_not_told(self):
        UserProfile.objects.filter(pk=self.scm2.pk).update(is_active=False)
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        self.assertFalse(Notification.objects.filter(recipient=self.scm2).exists())

    def test_second_damaging_submission_reuses_the_open_issue_with_a_comment(self):
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        issue = Issue.objects.get()
        Notification.objects.all().delete()
        response = self.confirm({self.panels: ('10', '2', []), self.inverter: ('2', '1', [jpg('inv.jpg')])})
        self.assertEqual(Issue.objects.count(), 1)
        comment = Comment.objects.get()
        self.assertEqual((comment.issue, comment.author), (issue, self.se))
        self.assertEqual(comment.body,
                         f'{_name(self.se)} recorded damage on Inverter — Inverter 5kW: 1 damaged')
        self.assertEqual(set(DCLineDamagePhoto.objects.values_list('issue_id', flat=True)), {issue.pk})
        self.assertEqual(DCLineDamagePhoto.objects.filter(line=self.inverter).count(), 1)
        self.assertIn('The damage was added to the open damage issue for this challan.',
                      ' '.join(self.messages_of(response)))
        self.assertFalse(Notification.objects.exists(), 'a Comment notifies nobody')

    def test_one_comment_lists_every_line_recording_damage_now(self):
        issue = self.damage_issue()
        self.confirm({self.panels: ('10', '3', []), self.inverter: ('2', '1', [jpg()])})
        # In the challan's line order (DCLineItem orders by boq_category).
        self.assertEqual(Comment.objects.get(issue=issue).body.splitlines(), [
            f'{_name(self.se)} recorded damage on Inverter — Inverter 5kW: 1 damaged',
            f'{_name(self.se)} recorded damage on Solar Modules — Panel 540Wp: 3 damaged',
        ])

    def test_a_resolved_issue_is_history_and_new_damage_raises_a_new_one(self):
        for status in (Issue.RESOLVED, Issue.CLOSED):
            with self.subTest(status=status):
                Issue.objects.filter(status__in=[Issue.OPEN, Issue.IN_PROGRESS]).update(status=status)
                old = self.damage_issue(status=status)
                response = self.confirm({self.inverter: ('2', '1', [jpg()])})
                self.assertTrue(any(m.startswith('Describe the damage') for m in self.messages_of(response)))
                self.confirm({self.inverter: ('2', '1', [jpg()])}, description='Inverter casing cracked')
                new = Issue.objects.exclude(pk=old.pk).get(status=Issue.OPEN)
                self.assertEqual(DCLineDamagePhoto.objects.filter(issue=new).count(), 1)
                Issue.objects.filter(pk=new.pk).update(status=status)
                DCLineItem.objects.filter(pk=self.inverter.pk).update(damaged_quantity=0)

    def test_an_in_progress_issue_is_still_the_damage_issue(self):
        issue = self.damage_issue(status=Issue.IN_PROGRESS)
        self.confirm({self.inverter: ('2', '1', [jpg()])})
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(Comment.objects.get().issue, issue)

    def test_a_withdrawn_photo_still_makes_its_issue_the_damage_issue(self):
        issue = Issue.objects.create(project=self.site, delivery_challan=self.dc,
                                     title='Damaged delivery — DC GD-1', status=Issue.OPEN)
        self.photo(self.panels, issue=issue, withdrawn_by=self.scm)
        self.confirm({self.inverter: ('2', '1', [jpg()])})
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(Comment.objects.get().issue, issue)

    def test_an_issue_without_photos_is_not_the_damage_issue(self):
        Issue.objects.create(project=self.site, delivery_challan=self.dc, title='Short delivery',
                             status=Issue.OPEN)
        response = self.confirm({self.inverter: ('2', '1', [jpg()])})
        self.assertTrue(any(m.startswith('Describe the damage') for m in self.messages_of(response)))

    def test_damage_set_to_zero_adds_a_comment_and_leaves_photos_and_issue(self):
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        issue = Issue.objects.get()
        photo = DCLineDamagePhoto.objects.get()
        self.override({self.panels: ('10', '0', [])})
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.damaged_quantity, 0)
        self.assertEqual(Comment.objects.get().body,
                         f'damage on Solar Modules — Panel 540Wp set to 0 by {_name(self.scm)}')
        issue.refresh_from_db()
        self.assertEqual(issue.status, Issue.OPEN)
        self.assertTrue(DCLineDamagePhoto.objects.filter(pk=photo.pk, withdrawn_at__isnull=True).exists())

    def test_damage_set_to_zero_with_no_open_issue_adds_nothing(self):
        self.damage_issue(status=Issue.RESOLVED)
        DCLineItem.objects.filter(pk=self.panels.pk).update(received_quantity=10, damaged_quantity=2,
                                                             grn_confirmed_by=self.se)
        self.override({self.panels: ('10', '0', [])})
        self.assertFalse(Comment.objects.exists())

    def test_no_pm_raises_the_issue_unassigned_and_still_tells_managers_and_scm(self):
        no_pm = _project('GD-NOPM')
        no_pm.coordinators.add(self.coord)
        phase = ProjectPhase.objects.create(project=no_pm, phase_name='Install', phase_order=1)
        Task.objects.create(phase=phase, task_name='Receive', task_order=1,
                            assigned_role=Task.SITE_ENGINEER, assigned_to=self.se)
        challan = DeliveryChallan.objects.create(project=no_pm, dc_number='GD-NP',
                                                 dc_date=date.today())
        line = DCLineItem.objects.create(challan=challan, boq_category='BOS',
                                         item_description='DC cable', ordered_quantity=5)
        response = self.confirm({line: ('5', '1', [jpg()])}, description='Cable drum crushed',
                                challan=challan, project=no_pm)
        issue = Issue.objects.get()
        self.assertIsNone(issue.assigned_to)
        self.assertIn('It is unassigned: this site has no active PM.', ' '.join(self.messages_of(response)))
        self.assertEqual(Notification.objects.filter(recipient=self.coord).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.scm).count(), 1)

    def test_an_inactive_pm_counts_as_no_pm(self):
        UserProfile.objects.filter(pk=self.pm.pk).update(is_active=False)
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        self.assertIsNone(Issue.objects.get().assigned_to)

    def test_several_photos_in_one_input_are_all_stored(self):
        self.confirm({self.panels: ('10', '2', [jpg('a.jpg'), jpg('b.jpg'),
                                                SimpleUploadedFile('c.png', PNG, content_type='image/png')])},
                     description='Two panels cracked')
        photos = DCLineDamagePhoto.objects.filter(line=self.panels)
        self.assertEqual(sorted(photos.values_list('original_name', flat=True)), ['a.jpg', 'b.jpg', 'c.png'])
        self.assertEqual(sorted(photos.values_list('mime', flat=True)),
                         ['image/jpeg', 'image/jpeg', 'image/png'])
        self.assertEqual(len(self.uploads()), 3)


# ---------------------------------------------------------------------------
# override_grn — the same rule, corrections
# ---------------------------------------------------------------------------

class OverrideTests(GrnDamageBase):

    def setUp(self):
        super().setUp()
        self.confirm({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        self.issue = Issue.objects.get()
        self.bucket_api.upload.reset_mock()

    def test_same_photo_rule_on_override(self):
        response = self.override({self.inverter: ('2', '1', [])},
                                 reason='Site engineer unreachable today')
        self.assertTrue(any('Inverter — Inverter 5kW: 1 damaged — attach at least one photo'
                            in m for m in self.messages_of(response)))
        self.inverter.refresh_from_db()
        self.assertIsNone(self.inverter.received_quantity)

    def test_a_correction_keeping_damage_on_a_photographed_line_needs_no_new_photo(self):
        response = self.override({self.panels: ('9', '2', [])})
        self.panels.refresh_from_db()
        self.assertEqual(self.panels.received_quantity, Decimal('9'))
        self.assertEqual(self.uploads(), [])
        # Unchanged damage, no photos: not damage recorded now (QE) — no Comment.
        self.assertFalse(Comment.objects.exists())
        self.assertIn('GRN overridden. DC status: Rejected.', self.messages_of(response))

    def test_changing_the_damage_on_a_photographed_line_comments_without_a_photo(self):
        self.override({self.panels: ('10', '3', [])})
        self.assertEqual(Comment.objects.get(issue=self.issue).body,
                         f'{_name(self.scm)} recorded damage on Solar Modules — Panel 540Wp: 3 damaged')

    def test_after_the_issue_is_resolved_an_unchanged_damaged_line_needs_no_new_issue(self):
        Issue.objects.filter(pk=self.issue.pk).update(status=Issue.RESOLVED)
        response = self.override({self.panels: ('9', '2', []), self.inverter: ('2', '0', [])},
                                 reason='Inverter checked at the warehouse')
        self.assertEqual(Issue.objects.count(), 1)
        self.inverter.refresh_from_db()
        self.assertEqual(self.inverter.received_quantity, Decimal('2'))
        self.assertFalse(any('Describe the damage' in m for m in self.messages_of(response)))


# ---------------------------------------------------------------------------
# R8 — uploads and the transaction
# ---------------------------------------------------------------------------

class AllOrNothingTests(GrnDamageBase):

    def test_a_database_failure_discards_every_upload(self):
        with mock.patch('projects.views.recalculate_dc_status', side_effect=RuntimeError('db down')):
            with self.assertRaises(RuntimeError):
                self.confirm({self.panels: ('10', '2', [jpg('a.jpg'), jpg('b.jpg')])},
                             description='Two panels cracked')
        self.assertFalse(Issue.objects.exists())
        self.assertFalse(DCLineDamagePhoto.objects.exists())
        self.panels.refresh_from_db()
        self.assertIsNone(self.panels.received_quantity)
        uploaded = self.uploads()
        self.assertEqual(len(uploaded), 2)
        self.assertEqual(self.removes(), [mock.call([p]) for p in uploaded])

    def test_a_failed_upload_part_way_discards_what_was_stored(self):
        self.bucket_api.upload.side_effect = [None, RuntimeError('storage down')]
        response = self.confirm({self.panels: ('10', '2', [jpg('a.jpg'), jpg('b.jpg')])},
                                description='Two panels cracked')
        self.assertIn('Upload to storage failed: RuntimeError. Nothing was saved.',
                      self.messages_of(response))
        self.assertEqual(self.removes(), [mock.call([self.uploads()[0]])])
        self.assertFalse(Issue.objects.exists())
        self.assertIn('grn_draft', self.client.session)

    def test_the_issue_closed_meanwhile_is_a_clean_refusal(self):
        """QK: the page found an open damage Issue, so no description was asked; it was
        resolved before this GRN took the lock."""
        issue = self.damage_issue()
        real = grn_damage.upload_planned_photos

        def resolve_then_upload(plan, project):
            Issue.objects.filter(pk=issue.pk).update(status=Issue.RESOLVED)
            return real(plan, project)

        with mock.patch('projects.views.upload_planned_photos', side_effect=resolve_then_upload):
            response = self.confirm({self.inverter: ('2', '1', [jpg()])})
        self.assertIn('The damage issue on this challan was resolved or closed while you were '
                      'working. Describe the damage to raise a new one.', self.messages_of(response))
        self.assertEqual(Issue.objects.count(), 1)
        self.inverter.refresh_from_db()
        self.assertIsNone(self.inverter.received_quantity)
        self.assertEqual(self.removes(), [mock.call([self.uploads()[0]])])
        self.assertEqual(DCLineDamagePhoto.objects.count(), 1)      # the fixture's own
        self.assertIn('grn_draft', self.client.session)

    def test_an_issue_raised_meanwhile_is_reused_with_the_description_in_the_comment(self):
        real = grn_damage.upload_planned_photos
        holder = {}

        def raise_then_upload(plan, project):
            holder['issue'] = self.damage_issue(photo_line=self.inverter)
            return real(plan, project)

        with mock.patch('projects.views.upload_planned_photos', side_effect=raise_then_upload):
            self.confirm({self.panels: ('10', '1', [jpg()])}, description='Corner of one panel chipped')
        self.assertEqual(Issue.objects.count(), 1)
        self.assertEqual(Comment.objects.get(issue=holder['issue']).body.splitlines(), [
            'Corner of one panel chipped',
            f'{_name(self.se)} recorded damage on Solar Modules — Panel 540Wp: 1 damaged',
        ])

    def test_a_grn_with_no_damage_is_unchanged(self):
        response = self.confirm({self.panels: ('10', '0', []), self.inverter: ('2', '0', [])})
        self.assertIn('GRN confirmed. DC status: Received.', self.messages_of(response))
        self.assertFalse(Issue.objects.exists())
        self.assertFalse(Comment.objects.exists())
        self.assertEqual(self.uploads(), [])
        self.assertNotIn('grn_draft', self.client.session)


# ---------------------------------------------------------------------------
# Open and withdraw
# ---------------------------------------------------------------------------

class OpenAndWithdrawTests(GrnDamageBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.issue = Issue.objects.create(project=cls.site, delivery_challan=cls.dc,
                                         title='Damaged delivery — DC GD-1', status=Issue.OPEN)
        cls.live = cls.photo(cls.panels, issue=cls.issue, name='live.jpg')
        cls.gone = cls.photo(cls.panels, issue=cls.issue, name='gone.jpg', withdrawn_by=cls.scm)

    def open(self, profile, photo):
        self.login(profile)
        return self.client.get(reverse('dc_damage_photo_open',
                                       args=[self.site.project_id, self.dc.pk, photo.pk]))

    def withdraw(self, profile, photo, reason='Wrong crate in the photo'):
        self.login(profile)
        return self.client.post(reverse('dc_damage_photo_withdraw',
                                        args=[self.site.project_id, self.dc.pk, photo.pk]),
                                {'withdrawn_reason': reason})

    def test_the_site_engineer_opens_a_live_photo_and_the_open_is_logged(self):
        response = self.open(self.se, self.live)
        self.assertRedirects(response, SIGNED, fetch_redirect_response=False)
        self.bucket_api.create_signed_url.assert_called_once_with(self.live.path, 300)
        row = ActivityLog.objects.get(action_code='dc_damage_photo_opened')
        self.assertEqual((row.entity_type, row.entity_id, row.action),
                         ('DCLineDamagePhoto', self.live.pk, 'Opened a damage photo on DC GD-1'))

    def test_any_project_viewer_opens_a_live_photo(self):
        for profile in (self.finance, self.pm, self.coord, self.admin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.open(profile, self.live).status_code, 302)

    def test_an_unrelated_user_gets_a_404(self):
        self.assertEqual(self.open(self.se_elsewhere, self.live).status_code, 404)
        self.assertFalse(ActivityLog.objects.filter(action_code='dc_damage_photo_opened').exists())

    def test_a_withdrawn_photo_is_a_404_outside_scm_and_admin(self):
        for profile in (self.se, self.pm, self.finance):
            with self.subTest(role=profile.role):
                self.assertEqual(self.open(profile, self.gone).status_code, 404)
        for profile in (self.scm, self.admin):
            with self.subTest(role=profile.role):
                self.assertEqual(self.open(profile, self.gone).status_code, 302)

    def test_another_challans_photo_is_a_404(self):
        other_dc = DeliveryChallan.objects.create(project=self.site, dc_number='GD-2',
                                                  dc_date=date.today())
        self.login(self.scm)
        response = self.client.get(reverse('dc_damage_photo_open',
                                           args=[self.site.project_id, other_dc.pk, self.live.pk]))
        self.assertEqual(response.status_code, 404)

    def test_opens_are_hidden_from_recent_activity(self):
        self.open(self.se, self.live)
        self.withdraw(self.scm, self.live)
        response = self.client.get(reverse('project_overview', args=[self.site.project_id]))
        codes = [row.action_code for row in response.context['recent_activity']]
        self.assertNotIn('dc_damage_photo_opened', codes)
        self.assertIn('dc_damage_photo_withdrawn', codes)
        self.assertTrue(ActivityLog.objects.filter(action_code='dc_damage_photo_opened').exists())

    def test_scm_and_admin_withdraw_and_the_object_is_kept(self):
        for profile, photo in ((self.scm, self.live),
                               (self.admin, self.photo(self.inverter, issue=self.issue))):
            with self.subTest(role=profile.role):
                response = self.withdraw(profile, photo)
                self.assertEqual(response.status_code, 302)
                photo.refresh_from_db()
                self.assertEqual((photo.withdrawn_by, photo.withdrawn_reason),
                                 (profile, 'Wrong crate in the photo'))
        self.bucket_api.remove.assert_not_called()
        self.assertEqual(self.open(self.se, self.live).status_code, 404)

    def test_others_cannot_withdraw(self):
        for profile in (self.se, self.pm, self.coord, self.finance):
            with self.subTest(role=profile.role):
                self.assertEqual(self.withdraw(profile, self.live).status_code, 403)
        self.assertEqual(self.withdraw(self.se_elsewhere, self.live).status_code, 404)
        self.live.refresh_from_db()
        self.assertTrue(self.live.is_live)

    def test_a_short_reason_is_refused(self):
        self.withdraw(self.scm, self.live, reason='wrong')
        self.live.refresh_from_db()
        self.assertTrue(self.live.is_live)

    def test_an_already_withdrawn_photo_is_refused(self):
        response = self.withdraw(self.scm, self.gone)
        self.assertIn('gone.jpg was already withdrawn.', self.messages_of(response))

    def test_get_is_not_allowed(self):
        self.login(self.scm)
        response = self.client.get(reverse('dc_damage_photo_withdraw',
                                           args=[self.site.project_id, self.dc.pk, self.live.pk]))
        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# The two pages
# ---------------------------------------------------------------------------

class PageTests(GrnDamageBase):

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.issue = Issue.objects.create(project=cls.site, delivery_challan=cls.dc,
                                         title='Damaged delivery — DC GD-1', status=Issue.OPEN)
        cls.live = cls.photo(cls.panels, issue=cls.issue, name='live.jpg')
        cls.gone = cls.photo(cls.panels, issue=cls.issue, name='gone.jpg', withdrawn_by=cls.scm)

    def detail(self, profile):
        self.login(profile)
        return self.client.get(reverse('delivery_challan_detail',
                                       args=[self.site.project_id, self.dc.pk]))

    def issue_page(self, profile):
        self.login(profile)
        return self.client.get(reverse('issue_detail', args=[self.issue.pk]))

    def _count(self, fn, profile):
        fn(profile)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(fn(profile).status_code, 200)
        return len(ctx)

    def test_dc_page_lists_live_photos_and_hides_withdrawn_ones_from_se_and_pm(self):
        for profile in (self.se, self.pm):
            with self.subTest(role=profile.role):
                page = self.detail(profile).content.decode()
                self.assertIn('live.jpg', page)
                self.assertNotIn('gone.jpg', page)
                self.assertNotIn('dc_damage_photo_withdraw', page)
                self.assertNotIn('/withdraw/" ', page)
        page = self.detail(self.scm).content.decode()
        self.assertIn('gone.jpg', page)
        self.assertIn(reverse('dc_damage_photo_withdraw',
                              args=[self.site.project_id, self.dc.pk, self.live.pk]), page)

    def test_dc_page_says_damage_goes_to_the_open_issue_and_asks_no_description(self):
        page = self.detail(self.se).content.decode()
        self.assertIn('Damage you record now is added to the open damage issue', page)
        self.assertNotIn('name="damage_description"', page)
        Issue.objects.filter(pk=self.issue.pk).update(status=Issue.RESOLVED)
        page = self.detail(self.se).content.decode()
        self.assertIn('name="damage_description"', page)

    def test_both_grn_forms_post_files_and_carry_one_photo_input_per_line(self):
        for profile, url_name in ((self.se, 'confirm_grn'), (self.scm, 'override_grn')):
            with self.subTest(form=url_name):
                page = self.detail(profile).content.decode()
                action = reverse(url_name, args=[self.site.project_id, self.dc.pk])
                start = page.rindex('<form', 0, page.index(f'action="{action}"'))
                form = page[start:page.index('</form>', start)]
                self.assertIn('enctype="multipart/form-data"', form)
                for line in (self.panels, self.inverter):
                    tag = re.search(rf'<input[^>]*name="damage_photos_{line.pk}"[^>]*>', form).group(0)
                    self.assertIn(' multiple', tag)
                    self.assertIn('accept="image/jpeg,image/png,.jpg,.jpeg,.png"', tag)
                    self.assertIn('data-qaqc-photo', tag)
                    self.assertNotIn('capture', tag, 'gallery allowed (R9)')
                    self.assertEqual(page.count(f'name="damage_photos_{line.pk}"'), 1)
                self.assertIn('Preparing photo', page, 'the shared photo script is included')

    def test_issue_page_lists_photos_by_line_with_open_links_for_any_viewer(self):
        for profile in (self.finance, self.se, self.pm):
            with self.subTest(role=profile.role):
                page = self.issue_page(profile).content.decode()
                self.assertIn('Damage photos', page)
                self.assertIn(reverse('dc_damage_photo_open',
                                      args=[self.site.project_id, self.dc.pk, self.live.pk]), page)
                self.assertNotIn('gone.jpg', page)
        page = self.issue_page(self.scm).content.decode()
        self.assertIn('gone.jpg', page)

    def test_dc_page_costs_one_query_more_at_most(self):
        for profile in (self.se, self.scm, self.pm):
            with self.subTest(role=profile.role):
                after = self._count(self.detail, profile)
                with mock.patch('projects.views.DCLineDamagePhoto'):
                    before = self._count(self.detail, profile)
                self.assertLessEqual(after - before, 1)

    def test_more_photos_cost_nothing_more_on_either_page(self):
        dc_before = self._count(self.detail, self.scm)
        issue_before = self._count(self.issue_page, self.scm)
        for n in range(3):
            self.photo(self.inverter, issue=self.issue, name=f'x{n}.jpg', uploader=self.scm2)
        self.assertEqual(self._count(self.detail, self.scm), dc_before)
        self.assertEqual(self._count(self.issue_page, self.scm), issue_before)

    def test_issue_page_costs_one_query_more_at_most_and_none_for_other_issues(self):
        for profile in (self.se, self.scm, self.pm):
            with self.subTest(role=profile.role):
                after = self._count(self.issue_page, profile)
                with mock.patch('projects.views.DCLineDamagePhoto'):
                    before = self._count(self.issue_page, profile)
                self.assertLessEqual(after - before, 1)
        plain = Issue.objects.create(project=self.site, title='Gate lock broken')
        self.login(self.pm)
        url = reverse('issue_detail', args=[plain.pk])
        self.client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(url)
        self.assertFalse([q for q in ctx.captured_queries
                          if 'dclinedamagephoto' in q['sql'].lower()])


# ---------------------------------------------------------------------------
# Model, registry, the shared photo script's other callers
# ---------------------------------------------------------------------------

class ModelTests(GrnDamageBase):

    def _row(self, **fields):
        values = dict(line=self.panels, uploaded_by=self.se, original_name='x.jpg',
                      bucket=BUCKET, path='GD-SITE/grn-damage/x.jpg', size=1, mime='image/jpeg')
        values.update(fields)
        return DCLineDamagePhoto(**values)

    def test_mime_must_be_an_image(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(mime='application/pdf').save()

    def test_withdrawal_is_when_who_and_why_together(self):
        for fields in ({'withdrawn_at': timezone.now(), 'withdrawn_by': self.scm},
                       {'withdrawn_at': timezone.now(), 'withdrawn_reason': 'Wrong crate'},
                       {'withdrawn_by': self.scm}, {'withdrawn_reason': 'Wrong crate'}):
            with self.subTest(fields=sorted(fields)):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self._row(**fields).save()

    def test_withdrawn_after_uploaded(self):
        now = timezone.now()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._row(uploaded_at=now, withdrawn_at=now - timedelta(minutes=1),
                      withdrawn_by=self.scm, withdrawn_reason='Wrong crate').save()

    def test_append_only_and_protected(self):
        row = self._row()
        row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.save()
        with self.assertRaises(AppendOnlyViolation):
            row.delete()
        with self.assertRaises(ProtectedError):
            self.panels.delete()

    def test_the_registry_names_the_scm_recipients(self):
        self.assertIn('every active SCM user is also told (in-app and email)',
                      NOTIFICATION_EVENTS['issue_raised'].recipients_rule)


class PhotoScriptCallerTests(TestCase):
    """The QA/QC visit and punch-point forms still carry what _qaqc_photo_script.html
    reads when an input names no target of its own: a photo_taken_at and a
    .qaqc-photo-status in the same form, and a single-file input. Read from the source:
    rendering them needs a visit and a punch point, which their own suites build."""

    TEMPLATES = Path(settings.BASE_DIR) / 'projects' / 'templates' / 'projects'

    def forms_with_photo(self, relative):
        source = (self.TEMPLATES / relative).read_text(encoding='utf-8')
        forms = re.findall(r'<form\b.*?</form>', source, re.S)
        return [f for f in forms if 'data-qaqc-photo' in f]

    def test_qaqc_point_and_punch_forms_still_carry_the_scripts_targets(self):
        for relative, expected in (('partials/_qaqc_point.html', 1),
                                   ('partials/_punch_points.html', 2)):
            forms = self.forms_with_photo(relative)
            self.assertEqual(len(forms), expected, relative)
            for form in forms:
                with self.subTest(template=relative):
                    self.assertIn('name="photo_taken_at"', form)
                    self.assertIn('qaqc-photo-status', form)
                    tag = re.search(r'<input[^>]*data-qaqc-photo[^>]*>', form, re.S).group(0)
                    self.assertNotIn('multiple', tag)
                    self.assertNotIn('data-qaqc-photo-status', tag)

    def test_both_pages_still_include_the_script(self):
        for relative in ('qaqc_visit.html', 'task_detail.html'):
            source = (self.TEMPLATES / relative).read_text(encoding='utf-8')
            self.assertIn("{% include 'projects/partials/_qaqc_photo_script.html' %}", source)

    def test_the_script_keeps_the_form_wide_fallback(self):
        source = (self.TEMPLATES / 'partials' / '_qaqc_photo_script.html').read_text(encoding='utf-8')
        self.assertIn("target(input, 'data-qaqc-photo-taken', 'input[name=\"photo_taken_at\"]')", source)
        self.assertIn("target(input, 'data-qaqc-photo-status', '.qaqc-photo-status')", source)
