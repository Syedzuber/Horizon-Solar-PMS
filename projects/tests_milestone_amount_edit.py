"""FIN-M-B1b — the PM milestone edit on the project page never wipes an amount silently.

WHY THIS FILE EXISTS
--------------------
The PM branch of project_overview's update_milestone turned a blank, unparseable or
comma-formatted amount into None and reported "M1 updated.", erasing the amount BD set
from the final proposal (ruling R6: BD owns M1-M3 amounts; amounts must not be lost).
It also stored a negative amount, rounded a third decimal place, and 500'd on "NaN" or a
number too long for the column.

Rulings (Zuber, 12 Nov 2026):
* A refused amount refuses the WHOLE submit — description and due date are not saved
  either — the same as the existing bad-date rule in this branch.
* Blank means "no change": the stored amount is kept. A PM can no longer clear an amount.
* Commas are refused, matching BD's set_milestone_amounts.
* The shared number_input.parse_decimal_input reads the amount (places=2, min 0, max =
  the column), and its own message is shown, prefixed "M1 not updated.".
* Zero is allowed. The form renders a stored 0 as an empty box, so a description-only
  edit of a 0 milestone posts a blank amount — that must keep the 0.

Run with:
    python manage.py test projects.tests_milestone_amount_edit --settings=solarpms.test_settings
"""
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.contrib.messages import constants as message_constants, get_messages
from django.test import Client, TestCase
from django.urls import reverse

from .models import PaymentMilestone, Project

STORED_AMOUNT = Decimal('150000.00')
STORED_DESCRIPTION = 'Advance'
STORED_DUE = date(2026, 10, 15)


def _profile(username, role):
    """A post_save signal creates the UserProfile; fetch and set, never create."""
    user = User.objects.create_user(username=username, password='x')
    profile = user.profile
    profile.role = role
    profile.is_active = True
    profile.save()
    return profile


class PMMilestoneAmountEditTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.pm = _profile('ms_amt_pm', 'PM')
        cls.project = Project.objects.create(
            customer_name='Milestone Amount', status='Active', project_type='Residential',
            customer_phone='9876543210', site_address='1 Sun Road', city='Lucknow',
            state='Uttar Pradesh', dc_capacity_kw=Decimal('5.00'), assigned_pm=cls.pm,
            cascade_scheduling=False)

    def setUp(self):
        self.milestone = PaymentMilestone.objects.create(
            project=self.project, milestone_name='M1',
            milestone_description=STORED_DESCRIPTION, amount=STORED_AMOUNT,
            due_date=STORED_DUE, created_by=self.pm)
        self.client = Client(SERVER_NAME='localhost')
        self.client.force_login(self.pm.user)

    def submit(self, amount, description='Changed note', due_date='2026-11-20'):
        """POST the PM edit form exactly as project_overview.html builds it."""
        return self.client.post(
            reverse('project_overview', args=[self.project.project_id]),
            {'action': 'update_milestone', 'milestone_pk': self.milestone.pk,
             'milestone_description': description, 'amount': amount, 'due_date': due_date})

    def row(self):
        return PaymentMilestone.objects.get(pk=self.milestone.pk)

    @staticmethod
    def queued(response):
        return [(m.level, str(m)) for m in get_messages(response.wsgi_request)]

    def assert_refused_unchanged(self, amount, expected_fragment):
        """The whole submit is refused: amount, description and due date all as stored,
        an error that starts 'M1 not updated.' and names the problem, and no 'updated'."""
        response = self.submit(amount)
        self.assertEqual(response.status_code, 302)
        row = self.row()
        self.assertEqual(row.amount, STORED_AMOUNT)
        self.assertEqual(row.milestone_description, STORED_DESCRIPTION)
        self.assertEqual(row.due_date, STORED_DUE)
        msgs = self.queued(response)
        errors = [text for level, text in msgs if level == message_constants.ERROR]
        self.assertEqual(len(errors), 1, msgs)
        self.assertTrue(errors[0].startswith('M1 not updated. Amount:'), errors[0])
        self.assertIn(expected_fragment, errors[0])
        self.assertNotIn('M1 updated.', [text for _, text in msgs])

    # --- refused, nothing changes --------------------------------------------------

    def test_text_is_refused_and_keeps_the_amount(self):
        self.assert_refused_unchanged('abc', 'is not a number')

    def test_indian_commas_are_refused(self):
        self.assert_refused_unchanged('1,50,000', 'is not a number')

    def test_negative_is_refused(self):
        self.assert_refused_unchanged('-5000', 'cannot be negative')

    def test_nan_is_refused_not_a_server_error(self):
        self.assert_refused_unchanged('NaN', 'is not a number')

    def test_too_large_for_the_column_is_refused_not_a_server_error(self):
        self.assert_refused_unchanged('1e15', 'larger than the maximum')

    def test_third_decimal_place_is_refused_not_rounded(self):
        self.assert_refused_unchanged('150000.505', 'more than 2 decimal places')

    # --- blank = no change ------------------------------------------------------------

    def test_clearing_the_box_keeps_the_amount_and_saves_the_rest(self):
        response = self.submit('')
        row = self.row()
        self.assertEqual(row.amount, STORED_AMOUNT)
        self.assertEqual(row.milestone_description, 'Changed note')
        self.assertEqual(row.due_date, date(2026, 11, 20))
        self.assertIn('M1 updated.', [text for _, text in self.queued(response)])

    def test_zero_is_saved_and_survives_a_description_only_edit(self):
        """The form renders a stored 0 as an empty box, so the second save posts ''."""
        self.submit('0')
        self.assertEqual(self.row().amount, Decimal('0'))
        self.submit('', description='Only the note changed')
        row = self.row()
        self.assertEqual(row.amount, Decimal('0'))
        self.assertEqual(row.milestone_description, 'Only the note changed')

    # --- still works ------------------------------------------------------------------

    def test_a_valid_number_saves(self):
        response = self.submit('175000.50')
        row = self.row()
        self.assertEqual(row.amount, Decimal('175000.50'))
        self.assertEqual(row.milestone_description, 'Changed note')
        self.assertEqual(row.due_date, date(2026, 11, 20))
        self.assertIn('M1 updated.', [text for _, text in self.queued(response)])

    def test_a_blank_due_date_still_clears_it(self):
        """Unchanged by this session: blank means 'clear' for the date, not the amount."""
        self.submit('', due_date='')
        row = self.row()
        self.assertIsNone(row.due_date)
        self.assertEqual(row.amount, STORED_AMOUNT)
