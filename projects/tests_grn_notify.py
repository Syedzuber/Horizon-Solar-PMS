"""
NTF-DCG 5c — the goods-receipt email (event grn_recorded, rulings E1-E9; go-ahead Q1-Q10).

  * E1-E3: one send per receipt or correction, after commit, from write_goods_receipt;
    SCM (all active) + the site's assigned PM (not coordinators) + named recipients; the
    actor dropped; imported, refused and rolled-back GRNs send nothing; paused email
    leaves 'skipped' rows while in-app goes out.
  * E4-E6: subject, "Correction: " prefix and on-behalf reason, header facts, the
    three-column material table (Q8), the damage section with the Issue number and title
    and no URL (Q5), documents counted by type, PO "not linked", vendor "Not recorded",
    the in-app line, no money anywhere.
  * E7: a GRN-raised Issue is sent as create_delivery_issue sends one — SCM not told.
  * E8: the table block — every cell escaped, plain-text form, inline use is text,
    an empty section drops its paragraph, the samples render a table (Q7).
  * E5/Q4: the link anchors to the receipt; the page carries the anchor and the opener
    script; a named Finance recipient can open it (5a).

Runs with --settings=solarpms.test_settings (NOTIFY_DEFERRED_SYNC; migrations off).
No send leaves the process: the master switches are off and GrnDamageBase fails any
HTTP request.
"""

from datetime import date
from unittest import mock

from django.urls import reverse

from . import grn_notify, notify_deferred
from .models import (
    GOODS_RECEIPT_IMPORTED, RECEIPT_DOCUMENT_PHOTO, RECEIPT_DOCUMENT_SIGNED_DC, Comment,
    DCLineItem, GoodsReceipt, Issue, Notification, NotificationEventSetting, NotificationLog,
    UserProfile, Vendor,
)
from .notify import render_body_html, render_body_text, render_email
from .notify_admin import add_named_recipient
from .notify_registry import NOTIFICATION_EVENTS, EmailTable, get_event
from .tests_grn_damage import jpg
from .tests_receipt_attachments import ReceiptDocumentBase, pdf, photo

EVENT = 'grn_recorded'
MONEY_WORDS = ('rate', 'price', 'amount', 'cost', 'value', 'gst', 'total', '₹', 'rs.')


class GrnNotifyBase(ReceiptDocumentBase):
    """ReceiptDocumentBase's site, challan and people: gd_scm, gd_scm2, gd_scmco (SCM),
    gd_pm (assigned PM), gd_coord (coordinator), gd_se (holds the receiving task)."""

    def setUp(self):
        super().setUp()
        self.deferred = []
        patcher = mock.patch.object(notify_deferred, 'defer', side_effect=self._capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _capture(self, *, recipient, channel, row_fields, send_kwargs):
        self.deferred.append((recipient, channel, send_kwargs))
        return 0

    def email_on(self):
        NotificationEventSetting.objects.create(event_code=EVENT, channel='email',
                                                is_active=True, changed_by=self.admin,
                                                reason='Switched on for the test')

    def emails(self):
        """{username: send_kwargs} of the deferred grn_recorded emails."""
        return {r.user.username: kw for r, channel, kw in self.deferred if channel == 'email'}

    def one_email(self):
        emails = self.emails()
        self.assertTrue(emails, 'no email was handed to the deferred pool')
        return next(iter(emails.values()))

    def grn_in_app(self, profile):
        return list(Notification.objects.filter(recipient=profile,
                                                message__startswith='GRN-'))

    def detail_url(self):
        return reverse('delivery_challan_detail', args=[self.site.project_id, self.dc.pk])


# ---------------------------------------------------------------------------
# E1-E3 — who, when, once
# ---------------------------------------------------------------------------

class RecipientTests(GrnNotifyBase):

    def test_a_receipt_sends_one_email_per_recipient_scm_all_and_the_pm(self):
        self.email_on()
        self.confirm_docs({self.panels: ('10', '0', [])})
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(sorted(self.emails()), ['gd_pm', 'gd_scm', 'gd_scm2', 'gd_scmco'])
        # Coordinators are not included (E2), nor the SE who recorded it.
        self.assertNotIn('gd_coord', self.emails())
        self.assertNotIn('gd_se', self.emails())
        for profile in (self.scm, self.scm2, self.scm_coord, self.pm):
            with self.subTest(user=profile.user.username):
                [note] = self.grn_in_app(profile)
                self.assertEqual(note.message,
                                 f'{receipt.number} recorded on DC GD-1 (GD-SITE) — '
                                 f'Partially Received.')
        self.assertEqual(self.grn_in_app(self.coord), [])

    def test_named_recipients_are_added(self):
        self.email_on()
        add_named_recipient(EVENT, self.finance, self.admin, 'Vendor payment reconciliation')
        self.confirm_docs({self.panels: ('10', '0', [])})
        self.assertEqual(sorted(self.emails()),
                         ['gd_fin', 'gd_pm', 'gd_scm', 'gd_scm2', 'gd_scmco'])

    def test_the_scm_actor_is_dropped_from_a_correction(self):
        self.email_on()
        self.override_docs({self.panels: ('10', '0', [])}, reason='Recorded at the warehouse gate')
        self.assertEqual(sorted(self.emails()), ['gd_pm', 'gd_scm2', 'gd_scmco'])

    def test_an_inactive_pm_is_not_sent_it(self):
        self.email_on()
        UserProfile.objects.filter(pk=self.pm.pk).update(is_active=False)
        self.confirm_docs({self.panels: ('10', '0', [])})
        self.assertNotIn('gd_pm', self.emails())
        self.assertEqual(sorted(self.emails()), ['gd_scm', 'gd_scm2', 'gd_scmco'])

    def test_each_receipt_sends_once(self):
        self.email_on()
        self.confirm_docs({self.panels: ('6', '0', [])})
        self.confirm_docs({self.panels: ('10', '0', [])})
        numbers = [kw['subject'].split(' · ')[0] for kw in
                   (kw for _r, ch, kw in self.deferred if ch == 'email')]
        self.assertEqual(sorted(set(numbers)), sorted(
            GoodsReceipt.objects.values_list('number', flat=True)))
        self.assertEqual(len(numbers), 2 * 4)

    def test_paused_email_writes_skipped_rows_and_in_app_still_goes(self):
        # The registry default: email paused until Admin switches it on (E1).
        self.confirm_docs({self.panels: ('10', '0', [])})
        self.assertEqual(self.deferred, [])
        rows = NotificationLog.objects.filter(event_code=EVENT, channel='email')
        self.assertEqual(sorted(r.recipient.user.username for r in rows),
                         ['gd_pm', 'gd_scm', 'gd_scm2', 'gd_scmco'])
        self.assertTrue(all((r.status, r.error_detail) == ('skipped', 'event paused')
                            for r in rows))
        self.assertEqual(NotificationLog.objects.filter(
            event_code=EVENT, channel='in_app', status='sent').count(), 4)

    def test_a_refused_grn_sends_nothing(self):
        self.email_on()
        self.confirm_docs({self.panels: ('', '', [])})      # G4: nothing to record
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertEqual(self.deferred, [])
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())

    def test_a_rolled_back_grn_sends_nothing(self):
        self.email_on()
        with mock.patch('projects.views.recalculate_dc_status',
                        side_effect=RuntimeError('database fault')):
            with self.assertRaises(RuntimeError):
                self.confirm_docs({self.panels: ('10', '0', [])})
        self.assertFalse(GoodsReceipt.objects.exists())
        self.assertEqual(self.deferred, [])
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())

    def test_an_imported_receipt_sends_nothing(self):
        self.email_on()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            imported = GoodsReceipt.objects.create(
                challan=self.dc, kind=GOODS_RECEIPT_IMPORTED, recorded_by=self.se,
                receipt_date=date.today())
            grn_notify.fire_after_commit(imported)
        self.assertEqual(callbacks, [])
        grn_notify.send_grn_recorded(imported.pk)   # refused at the send as well
        self.assertEqual(self.deferred, [])
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())


# ---------------------------------------------------------------------------
# E4-E6 — what it says
# ---------------------------------------------------------------------------

class ContentTests(GrnNotifyBase):

    def test_receipt_subject_header_and_table(self):
        self.email_on()
        self.confirm_docs({self.panels: ('6', '0', [])})
        receipt = GoodsReceipt.objects.get()
        email = self.one_email()
        self.assertEqual(email['subject'],
                         f'{receipt.number} · GD-SITE · DC GD-1 · Partially Received')
        text = email['message']
        today = date.today().strftime('%d %b %Y')
        for line in (f'GRN: {receipt.number} — Receipt',
                     f'Recorded by: Gd_Se Test on {today}',
                     'Site: GD-SITE — ',
                     f'Challan: DC GD-1 dated {today}',
                     'Vendor: Not recorded',
                     'PO/PI: not linked',
                     'Challan status after this receipt: Partially Received',
                     'Material',
                     # 5c review: no damage before or after, so no "Damaged" part.
                     f'1. Panel 540Wp — Unit: Nos · DC qty: 10 · Received: — → 6 (+6) · '
                     f'Condition: Good · Notes: note {self.panels.pk}',
                     'Documents: No documents attached'):
            with self.subTest(line=line):
                self.assertIn(line, text)
        self.assertNotIn('Damage\n', text, 'no damage section without a damaged line')
        self.assertNotIn('Damaged:', text)
        html = email['html_message']
        self.assertIn('<th', html)
        self.assertIn('Panel 540Wp<br>\nNos · DC qty 10', html)
        self.assertIn('— → 6 (+6)', html)
        self.assertIn(f'word-break:break-word;">Good<br>\nnote {self.panels.pk}</td>', html)
        self.assertNotIn('Damaged ', html)

    def test_a_line_with_damage_before_or_after_shows_the_damaged_change(self):
        self.email_on()
        # Damage recorded on the first receipt (after > 0)...
        self.confirm_docs({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        first = self.one_email()['message']
        self.assertIn('Received: — → 10 (+10) · Damaged: — → 2 (+2) · Condition: Partial', first)
        # ...and set back to 0 on the next (before > 0): the change still shows.
        self.deferred.clear()
        self.confirm_docs({self.panels: ('10', '0', [])})
        second = self.one_email()
        self.assertIn('Received: 10 → 10 (±0) · Damaged: 2 → 0 (−2) · Condition: Good',
                      second['message'])
        self.assertIn('Damaged 2 → 0 (−2)<br>\nGood', second['html_message'])

    def test_a_correction_has_the_prefix_and_the_on_behalf_reason(self):
        self.email_on()
        self.override_docs({self.panels: ('10', '0', [])}, reason='Recorded at the warehouse gate')
        receipt = GoodsReceipt.objects.get()
        email = self.one_email()
        self.assertEqual(email['subject'],
                         f'Correction: {receipt.number} · GD-SITE · DC GD-1 · Partially Received')
        self.assertIn(f'GRN: {receipt.number} — Correction (by SCM), recorded on behalf of '
                      f'the site team: Recorded at the warehouse gate', email['message'])

    def test_vendor_and_po_when_filled(self):
        vendor = Vendor.objects.create(name='Polycab India Ltd', contact_person='R', phone='1')
        type(self.dc).objects.filter(pk=self.dc.pk).update(vendor=vendor, po_number=' PO-778 ')
        self.email_on()
        self.confirm_docs({self.panels: ('10', '0', [])})
        text = self.one_email()['message']
        self.assertIn('Vendor: Polycab India Ltd', text)
        self.assertIn('PO/PI: PO-778', text)

    def test_documents_are_counted_by_type(self):
        self.email_on()
        self.confirm_docs({self.panels: ('10', '0', [])},
                          [(RECEIPT_DOCUMENT_PHOTO, photo('a.jpg')),
                           (RECEIPT_DOCUMENT_SIGNED_DC, pdf()),
                           (RECEIPT_DOCUMENT_PHOTO, photo('b.jpg'))])
        self.assertIn('Documents: Signed DC copy: 1 · Delivery photo: 2',
                      self.one_email()['message'])

    def test_a_damaged_line_adds_the_damage_section_with_issue_and_photo_count(self):
        self.email_on()
        self.confirm_docs({self.panels: ('10', '2', [jpg('a.jpg'), jpg('b.jpg')])},
                          description='Two panels cracked across the glass')
        issue = Issue.objects.get()
        email = self.one_email()
        text, html = email['message'], email['html_message']
        for part in ('Damage\n1. Panel 540Wp — Unit: Nos · Damaged: — → 2 (+2)',
                     f'Issue #{issue.pk}: {issue.title}',
                     'Damage photos linked to this receipt: 2'):
            with self.subTest(part=part):
                self.assertIn(part, text)
        self.assertIn(f'Issue #{issue.pk}: Damaged delivery — DC GD-1', html)
        # Q5: the Issue by number and title only; the one link is the challan's.
        self.assertNotIn('/issues/', text)
        self.assertNotIn('/issues/', html)

    def test_the_link_anchors_to_the_receipt(self):
        self.email_on()
        self.confirm_docs({self.panels: ('10', '0', [])})
        receipt = GoodsReceipt.objects.get()
        anchor = f'grn-{receipt.pk:06d}'
        link = f'{self.detail_url()}#{anchor}'
        [note] = self.grn_in_app(self.pm)
        self.assertEqual(note.link, link)
        email = self.one_email()
        self.assertRegex(email['html_message'],
                         rf'<a href="https?://[^"]+{link}" [^>]*>Open this receipt on the challan</a>')
        self.assertIn(f'Open this receipt on the challan: http', email['message'])
        self.assertIn(link, email['message'])
        page = self.detail(self.pm)
        self.assertIn(f'id="{anchor}"', page)
        self.assertIn('function openReceipt()', page)
        self.assertIn("window.addEventListener('hashchange', openReceipt)", page)

    def test_a_named_finance_recipient_can_open_the_link(self):
        add_named_recipient(EVENT, self.finance, self.admin, 'Vendor payment reconciliation')
        self.confirm_docs({self.panels: ('10', '0', [])})
        [note] = self.grn_in_app(self.finance)
        path, anchor = note.link.split('#')
        self.login(self.finance)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'id="{anchor}"', response.content.decode())

    def test_table_cells_are_escaped(self):
        DCLineItem.objects.filter(pk=self.panels.pk).update(
            item_description='<script>alert(1)</script> Panel & **bold**')
        self.email_on()
        data = self.payload({self.panels: ('10', '0', [])})
        data[f'grn_notes_{self.panels.pk}'] = '<img src=x onerror=alert(2)>'
        self.post('confirm_grn', data, self.se)
        html = self.one_email()['html_message']
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt; Panel &amp; **bold**', html)
        self.assertIn('&lt;img src=x onerror=alert(2)&gt;', html)

    def test_no_money_is_declared_or_rendered(self):
        event = get_event(EVENT)
        for variable in event.variables:
            for word in MONEY_WORDS:
                with self.subTest(variable=variable.name, word=word):
                    self.assertNotIn(word, variable.name.lower())
        self.email_on()
        self.confirm_docs({self.panels: ('10', '0', [])})
        receipt = GoodsReceipt.objects.get()
        values = grn_notify.grn_values(receipt)
        self.assertEqual(set(values), {v.name for v in event.variables})
        text = self.one_email()['message'].lower()
        for word in ('₹', 'rs.', 'price', 'amount', 'rate '):
            with self.subTest(word=word):
                self.assertNotIn(word, text)


# ---------------------------------------------------------------------------
# E7 — a GRN-raised Issue goes to the PM only; create_delivery_issue unchanged
# ---------------------------------------------------------------------------

class IssueNoticeTests(GrnNotifyBase):

    def issue_rows(self):
        """{(username, channel)} of every issue_raised row, plus the managers' nudges."""
        rows = {(r.recipient.user.username, r.channel)
                for r in NotificationLog.objects.filter(event_code='issue_raised')}
        nudges = {n.recipient.user.username for n in Notification.objects.filter(
            message__startswith='Issue "')}
        return rows, nudges

    def test_a_grn_raised_issue_reaches_the_pm_not_scm(self):
        self.confirm_docs({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        rows, nudges = self.issue_rows()
        self.assertEqual(rows, {('gd_pm', 'in_app'), ('gd_pm', 'email')})
        self.assertEqual(nudges, {'gd_coord', 'gd_scmco'})
        # SCM learn of it here instead: the GRN email's damage section.
        for profile in (self.scm, self.scm2):
            self.assertEqual(len(self.grn_in_app(profile)), 1)

    def test_create_delivery_issue_sends_exactly_what_it_did(self):
        self.login(self.se)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('create_delivery_issue',
                                     args=[self.site.project_id, self.dc.pk]),
                             {'title': 'Crate dented', 'description': 'Seen on unloading',
                              'severity': Issue.HIGH, 'assigned_to': str(self.pm.pk)})
        issue = Issue.objects.get()
        rows, nudges = self.issue_rows()
        # The assignee: issue_raised in-app and email (WhatsApp is its own legacy row);
        # every other manager: the in-app nudge; SCM nothing; the raiser nothing.
        self.assertEqual(rows, {('gd_pm', 'in_app'), ('gd_pm', 'email')})
        self.assertEqual(nudges, {'gd_coord', 'gd_scmco'})
        self.assertTrue(NotificationLog.objects.filter(
            recipient=self.pm, channel='whatsapp', template_name='issue_created').exists())
        self.assertFalse(Notification.objects.filter(recipient__in=[self.scm, self.scm2]).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.se).exists())
        self.assertEqual(Notification.objects.get(recipient=self.coord).link, f'/issues/{issue.pk}/')
        # No GRN, so no grn_recorded.
        self.assertFalse(NotificationLog.objects.filter(event_code=EVENT).exists())

    def test_reusing_the_open_issue_sends_no_issue_notice(self):
        self.confirm_docs({self.panels: ('10', '2', [jpg()])}, description='Two panels cracked')
        NotificationLog.objects.all().delete()
        Notification.objects.all().delete()
        self.confirm_docs({self.panels: ('10', '2', []), self.inverter: ('2', '1', [jpg('i.jpg')])})
        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(set(NotificationLog.objects.values_list('event_code', flat=True)), {EVENT})


# ---------------------------------------------------------------------------
# E8 — the table block
# ---------------------------------------------------------------------------

class TableBlockTests(GrnNotifyBase):

    TABLE = EmailTable(caption='Material', columns=('Item', 'Received'),
                       rows=(('<b>A</b>\nNos', '1 → 2 (+1)'),),
                       text_rows=('<b>A</b> — Received: 1 → 2 (+1)',),
                       footer=('Issue #3: <i>x</i>',))

    def render(self, body, value):
        allowed = ('t', 'recipient_name', 'link')
        return (render_body_html(body, {'t': value}, allowed),
                render_body_text(body, {'t': value}, allowed))

    def test_a_table_paragraph_draws_an_escaped_table_and_its_text(self):
        html, text = self.render('Before\n\n{{ t }}\n\nAfter', self.TABLE)
        self.assertIn('<table role="presentation"', html)
        self.assertIn('<strong>Material</strong>', html)
        self.assertIn('&lt;b&gt;A&lt;/b&gt;<br>\nNos', html)
        self.assertIn('Issue #3: &lt;i&gt;x&lt;/i&gt;', html)
        self.assertNotIn('<b>', html)
        self.assertEqual(text, 'Before\n\nMaterial\n1. <b>A</b> — Received: 1 → 2 (+1)\n'
                               'Issue #3: <i>x</i>\n\nAfter')

    def test_a_table_inside_a_sentence_is_escaped_text(self):
        html, _text = self.render('See {{ t }} here', self.TABLE)
        self.assertNotIn('<table', html)
        self.assertIn('&lt;b&gt;A&lt;/b&gt;', html)

    def test_an_empty_section_drops_its_paragraph(self):
        html, text = self.render('Before\n\n{{ t }}\n\nAfter', '')
        self.assertEqual(text, 'Before\n\nAfter')
        self.assertEqual(html.count('<p'), 2)

    def test_a_malformed_table_is_refused(self):
        with self.assertRaises(ValueError):
            EmailTable(caption='', columns=('a', 'b'), rows=(('only one',),), text_rows=('x',))
        with self.assertRaises(ValueError):
            EmailTable(caption='', columns=('a',), rows=(('x',),), text_rows=())

    def test_the_samples_render_a_table_for_preview_and_test_send(self):
        event = get_event(EVENT)
        subject, text, html = render_email(event, None, event.samples(), '/')
        self.assertEqual(subject, 'GRN-000123 · UKRU001 · DC 004 · Partially Received')
        self.assertEqual(html.count('<table role="presentation" width="100%" cellpadding="0" '
                                    'cellspacing="0" style="width:100%; border-collapse'), 2)
        self.assertIn('1. 1C x 4 Sqmm DC solar cable', text)
        self.assertIn('Issue #45: Damaged delivery — DC 004', text)

    def test_the_admin_event_page_shows_the_event(self):
        self.login(self.admin)
        response = self.client.get(reverse('admin_notification_event', args=[EVENT]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Goods receipt recorded', response.content.decode())

    def test_registry_entry(self):
        event = NOTIFICATION_EVENTS[EVENT]
        self.assertEqual((event.channels, event.deferred, event.named_recipients,
                          event.default_state, event.whatsapp),
                         (('email',), True, True, 'paused', None))
