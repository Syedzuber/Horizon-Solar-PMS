"""
The project overview is never cached, so Back never shows stale task dates.

WHY: N/A and the ordinary approval are task-detail actions that can reschedule the site.
Coming Back to the overview used to be able to show the browser's stored copy, with the
due dates from before the action. never_cache makes the browser refetch it.

Run with:
    python manage.py test projects.tests_overview_never_cache --settings=solarpms.test_settings
"""
from django.urls import reverse

from .tests_sched2_scheduler import SchedFixture
from .tests_two_step_completion import _client_for


class OverviewNeverCacheTests(SchedFixture):

    def test_the_overview_tells_the_browser_never_to_reuse_it(self):
        site = self._site('40.00')
        response = _client_for(self.pm).get(reverse('project_overview', args=[site.project_id]))
        self.assertEqual(response.status_code, 200)
        directives = {d.strip() for d in response['Cache-Control'].split(',')}
        for directive in ('no-cache', 'no-store', 'must-revalidate'):
            self.assertIn(directive, directives)
