"""FIN-M-RT — Finance returns to the Finance work page after acting on a milestone.

WHY THIS FILE EXISTS
--------------------
Every action on /finance/work/ links to the project page's milestone card. Saving an
invoice or a receipt there used to land Finance on the project page, so they had to
find their way back (FIN-M-B8a report). And after a receipt, the task sync's "left as it
is" note read BEFORE "M3 marked as Received." (FIN-M-RN2 report).

Rulings (Zuber, 14 Nov 2026):
* The Finance work links carry ?return=finance_work&rq=<the page's own q/focus/ptype>,
  "Open site" rows too. The card's forms carry them as hidden fields.
* A save with a valid marker, by a user the Finance work page admits, returns there with
  the same filters. Only the fixed name is accepted, never a URL; rq is re-cleaned by the
  page's own rule, so unknown keys and bad values are dropped.
* A refusal stays on the project page with the error, reopened at the card with the
  marker kept, so a corrected second save still returns to Finance work.
* Without the marker, or for another role, nothing changes.
* milestone_receive honours the marker too (tested by direct POST; no screen sends it).
* Receipt messages: the success line first ("M3 marked as Received." — the pencil now
  says this when it recorded a receipt, "M3 updated." otherwise), then the no-invoice
  warning, then the task sync's messages.

Tests marked [changed] fail on the code before FIN-M-RT; the rest pin today's behaviour.

Run with:
    python manage.py test projects.tests_finance_return --settings=solarpms.test_settings
"""
from datetime import date
from unittest import mock
from urllib.parse import urlencode

from django.urls import reverse

from .models import PaymentMilestone, Task
from .tests_rn2_guards import (CONFIRMATION, SEND_INVOICE, SYNC_SKIPPED,
                               ResidentialFixture, _client, _profile, _texts)

#: The Finance work filters the tests travel with, and the query string they become.
FILTERS = {'q': 'RN2', 'focus': 'milestones', 'ptype': 'OPEX'}
FILTERS_QS = 'q=RN2&focus=milestones&ptype=OPEX'
MARKER = {'return': 'finance_work', 'rq': FILTERS_QS}

RECEIVED = 'M3 marked as Received.'
NO_INVOICE = ("M3 marked Received, but no invoice was recorded for it. "
              f"Its task '{SEND_INVOICE}' is still open.")


class FinanceReturnBase(ResidentialFixture):
    """RN2's Residential site (trigger, send-invoice and confirmation tasks; Pending M3),
    plus a CEO, who the Finance work page admits but who cannot save on the card."""

    def setUp(self):
        super().setUp()
        self.ceo = _profile('rt_ceo', 'CEO')
        self.overview = reverse('project_overview', args=[self.project.project_id])
        self.finance_work = reverse('finance_work')

    # -- the three save paths ------------------------------------------------

    def invoice(self, profile=None, amount='100000', **marker):
        return _client(profile or self.finance).post(
            reverse('milestone_invoice', args=[self.project.project_id, self.m3.pk]),
            {'invoice_number': 'RT-INV-1', 'invoice_date': date.today().isoformat(),
             'invoice_amount': amount, **marker})

    def pencil(self, profile=None, amount='100000', **marker):
        return _client(profile or self.finance).post(
            self.overview,
            {'action': 'update_milestone', 'milestone_pk': self.m3.pk,
             'milestone_description': 'Final', 'amount_received': amount, **marker})

    def receive(self, amount='100000', **marker):
        return _client(self.finance).post(
            reverse('milestone_receive', args=[self.project.project_id, self.m3.pk]),
            {'amount_received': amount, **marker})

    def invoiced(self):
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(status='Invoiced')

    def assert_goes_to(self, response, url):
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], url)

    def refused_url(self, rq=FILTERS_QS):
        return (self.overview + '?' + urlencode({'return': 'finance_work', 'rq': rq})
                + '#payment-milestones')


@mock.patch('projects.views.notify')
class FinanceWorkLinkTests(FinanceReturnBase):

    def setUp(self):
        super().setUp()
        # The trigger Done makes M3 due, so the page lists it under "to act on".
        Task.objects.filter(pk=self.trigger.pk).update(status=Task.DONE)

    def rows(self, profile, query):
        response = _client(profile).get(self.finance_work + query)
        self.assertEqual(response.status_code, 200)
        return {r.milestone.pk: r for r in response.context['to_act']}

    def test_links_carry_the_marker_and_only_the_known_filters(self, notify):  # [changed]
        expected = self.overview + '?' + urlencode(MARKER) + '#payment-milestones'
        for profile in (self.finance, self.ceo):     # "Open site" rows too (ruling)
            with self.subTest(role=profile.role):
                row = self.rows(profile, f'?{FILTERS_QS}&junk=1&focus2=x')[self.m3.pk]
                self.assertEqual(row.href, expected)

    def test_bad_filter_values_are_dropped_from_the_link(self, notify):  # [changed]
        row = self.rows(self.finance, '?q=RN2&focus=bogus&ptype=Nope')[self.m3.pk]
        self.assertEqual(row.href, self.overview + '?' + urlencode(
            {'return': 'finance_work', 'rq': 'q=RN2'}) + '#payment-milestones')


@mock.patch('projects.views.notify')
class ProjectPageHiddenFieldTests(FinanceReturnBase):

    def page(self, profile, query=''):
        response = _client(profile).get(self.overview + query)
        self.assertEqual(response.status_code, 200)
        return response

    def test_the_card_forms_carry_the_cleaned_marker(self, notify):  # [changed]
        response = self.page(self.finance, '?' + urlencode(
            {'return': 'finance_work', 'rq': FILTERS_QS + '&next=https://evil.example'}))
        self.assertEqual(response.context['finance_return'], MARKER)
        html = response.content.decode()
        # Invoice form and Finance pencil, one pair each.
        self.assertEqual(html.count('name="return" value="finance_work"'), 2)
        self.assertEqual(html.count(f'name="rq" value="{FILTERS_QS.replace("&", "&amp;")}"'),
                         2)
        self.assertNotIn('evil.example', html)

    def test_no_marker_without_one_or_with_a_tampered_one(self, notify):
        for query in ('', '?return=https://evil.example', '?return=/finance/work/',
                      '?return=Finance_Work'):
            with self.subTest(query=query):
                response = self.page(self.finance, query)
                # .get: the key does not exist before FIN-M-RT, and this pins today's page.
                self.assertIsNone(response.context.get('finance_return'))
                self.assertNotIn('name="return"', response.content.decode())

    def test_a_user_finance_work_does_not_admit_gets_no_marker(self, notify):
        response = self.page(self.pm, '?' + urlencode(MARKER))
        self.assertIsNone(response.context.get('finance_return'))
        self.assertNotIn('name="return"', response.content.decode())


@mock.patch('projects.views.notify')
class ReturnAfterSaveTests(FinanceReturnBase):

    def test_invoice_with_the_marker_returns_to_finance_work(self, notify):  # [changed]
        response = self.invoice(**MARKER)
        self.assert_goes_to(response, f'{self.finance_work}?{FILTERS_QS}')
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.INVOICED)

    def test_invoice_with_no_filters_returns_to_the_bare_page(self, notify):  # [changed]
        self.assert_goes_to(self.invoice(**{'return': 'finance_work', 'rq': ''}),
                            self.finance_work)

    def test_pencil_receipt_with_the_marker_returns_to_finance_work(self, notify):  # [changed]
        self.invoiced()
        response = self.pencil(**MARKER)
        self.assert_goes_to(response, f'{self.finance_work}?{FILTERS_QS}')
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.RECEIVED)

    def test_milestone_receive_with_the_marker_returns_to_finance_work(self, notify):  # [changed]
        self.invoiced()
        response = self.receive(**MARKER)
        self.assert_goes_to(response, f'{self.finance_work}?{FILTERS_QS}')
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.RECEIVED)

    def test_without_the_marker_every_path_stays_on_the_project_page(self, notify):
        self.assert_goes_to(self.invoice(), self.overview)
        self.assert_goes_to(self.pencil(), self.overview)
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(status='Pending')
        self.assert_goes_to(self.receive(), self.overview)

    def test_a_tampered_return_is_ignored(self, notify):
        for value in ('https://evil.example', '//evil.example', '/finance/work/',
                      'finance_work ', 'site_dashboard'):
            with self.subTest(value=value):
                PaymentMilestone.objects.filter(pk=self.m3.pk).update(
                    status='Pending', invoice_number='', invoice_date=None,
                    invoice_amount=None)
                self.assert_goes_to(self.invoice(**{'return': value, 'rq': FILTERS_QS}),
                                    self.overview)

    def test_extra_keys_in_rq_are_dropped_and_bad_values_cleaned(self, notify):  # [changed]
        rq = 'q=x&next=https://evil.example&focus=bogus&ptype=OPEX&return=evil'
        self.assert_goes_to(self.invoice(**{'return': 'finance_work', 'rq': rq}),
                            f'{self.finance_work}?q=x&ptype=OPEX')

    def test_rq_that_is_a_url_returns_to_the_bare_page(self, notify):  # [changed]
        response = self.invoice(**{'return': 'finance_work', 'rq': 'https://evil.example/'})
        self.assert_goes_to(response, self.finance_work)

    def test_an_over_long_rq_is_cut_not_a_server_error(self, notify):  # [changed]
        response = self.invoice(**{'return': 'finance_work', 'rq': 'a=1&' * 5000})
        self.assert_goes_to(response, self.finance_work)
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.INVOICED)

    def test_a_user_finance_work_does_not_admit_keeps_todays_behaviour(self, notify):
        # The PM's own pencil (manager branch) with a marker: back to the project page.
        response = _client(self.pm).post(
            self.overview, {'action': 'update_milestone', 'milestone_pk': self.m3.pk,
                            'milestone_description': 'Final', 'amount': '100000',
                            'due_date': '', **MARKER})
        self.assert_goes_to(response, self.overview)
        # And the PM refused on the Finance-only invoice: no marker on the way back.
        self.assert_goes_to(self.invoice(profile=self.pm, **MARKER), self.overview)
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)


@mock.patch('projects.views.notify')
class RefusalTests(FinanceReturnBase):
    """Ruling 3: a refusal stays on the project page, at the card, marker kept (6)."""

    def test_refused_invoice_stays_with_the_marker_kept(self, notify):  # [changed]
        response = self.invoice(amount='-5', **MARKER)
        self.assert_goes_to(response, self.refused_url())
        self.assertTrue(any(t.startswith('M3 not invoiced.') for t in _texts(response)))
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)

    def test_refused_pencil_and_receive_stay_with_the_marker_kept(self, notify):  # [changed]
        self.assert_goes_to(self.pencil(amount='abc', **MARKER), self.refused_url())
        self.assert_goes_to(self.receive(amount='', **MARKER), self.refused_url())
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.PENDING)

    def test_refused_without_the_marker_is_todays_redirect(self, notify):
        self.assert_goes_to(self.invoice(amount='-5'), self.overview)
        self.assert_goes_to(self.pencil(amount='abc'), self.overview)

    def test_a_corrected_second_save_returns_to_finance_work(self, notify):  # [changed]
        refused = self.invoice(amount='-5', **MARKER)
        # Reopen the page exactly where the refusal sent Finance...
        page = _client(self.finance).get(refused['Location'].split('#')[0])
        hidden = page.context['finance_return']
        self.assertEqual(hidden, MARKER)
        # ...and save again with the hidden fields the card now carries.
        self.assert_goes_to(self.invoice(**hidden), f'{self.finance_work}?{FILTERS_QS}')
        self.m3.refresh_from_db()
        self.assertEqual(self.m3.status, PaymentMilestone.INVOICED)


@mock.patch('projects.views.notify')
class ReceiptMessageOrderTests(FinanceReturnBase):
    """Success line first, then the no-invoice warning, then the task sync."""

    def assert_order(self, texts, *expected):
        positions = [texts.index(t) for t in expected]   # ValueError if one is missing
        self.assertEqual(positions, sorted(positions), texts)

    def test_milestone_receive_success_before_the_sync_note(self, notify):  # [changed]
        self.set_na(self.confirmation)
        self.invoiced()
        texts = _texts(self.receive())
        self.assert_order(texts, RECEIVED, SYNC_SKIPPED.format('M3', CONFIRMATION))

    def test_milestone_receive_from_pending_all_three_in_order(self, notify):  # [changed]
        self.set_na(self.confirmation)
        texts = _texts(self.receive())
        self.assert_order(texts, RECEIVED, NO_INVOICE,
                          SYNC_SKIPPED.format('M3', CONFIRMATION))

    def test_pencil_receipt_says_received_then_warning_then_sync(self, notify):  # [changed]
        self.set_na(self.confirmation)
        texts = _texts(self.pencil())
        self.assertNotIn('M3 updated.', texts)
        self.assert_order(texts, RECEIVED, NO_INVOICE,
                          SYNC_SKIPPED.format('M3', CONFIRMATION))

    def test_pencil_receipt_still_completes_an_applicable_task(self, notify):  # [changed]
        self.invoiced()
        texts = _texts(self.pencil())
        self.assertIn(RECEIVED, texts)
        self.confirmation.refresh_from_db()
        self.assertEqual(self.confirmation.status, Task.DONE)

    def test_pencil_description_only_still_says_updated(self, notify):
        PaymentMilestone.objects.filter(pk=self.m3.pk).update(
            status='Received', amount_received='100000')
        texts = _texts(self.pencil(amount='100000'))
        self.assertIn('M3 updated.', texts)
        self.assertNotIn(RECEIVED, texts)
