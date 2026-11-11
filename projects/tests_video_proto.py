"""
GRN-V0 — the throwaway video prototype (projects/video_proto.py, delete_video_proto).

The storage client is mocked everywhere (video_proto.bucket_api is patched), and every test
in StorageMockedBase also patches httpx and requests so ANY outbound HTTP fails the test.
Delete this module with the prototype.

What these tests cannot prove (no camera in a test): that a phone's camera opens, which
format it picks, the real bitrate and size, orientation, upload speed on site, or playback
on an iPhone. The field test is for those.
"""
import json
import re
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from projects import video_proto
from projects.models import ActivityLog

FLAG_ON = dict(VIDEO_PROTO_ENABLED=True, VIDEO_PROTO_TESTERS=['tess'],
               VIDEO_PROTO_BUCKET='video-proto-test')

HEX = '0123456789abcdef0123456789abcdef'
TESS_PATH = f'video-proto/tess/{HEX}.mp4'
OTHER_PATH = f'video-proto/otto/{HEX}.mp4'

def proto_logs():
    """The prototype's own log lines; force_login also writes a user_login line."""
    return ActivityLog.objects.filter(action_code__in=('video_proto_upload',
                                                       'video_proto_upload_failed'))


POST_ROUTES = ('video_proto_sign_upload', 'video_proto_sign_read', 'video_proto_log')
GET_ROUTES = ('video_proto_page', 'video_proto_results', 'video_proto_play')


class StorageMockedBase(TestCase):
    """Users, clients and a mocked bucket API; any real HTTP raises."""

    def setUp(self):
        for target in ('httpx.Client.send', 'requests.Session.send'):
            patcher = mock.patch(target, side_effect=AssertionError(f'HTTP attempted: {target}'))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.api = mock.MagicMock()
        self.api.create_signed_upload_url.return_value = {
            'signed_url': 'https://storage.invalid/upload?token=t', 'token': 't', 'path': ''}
        self.api.create_signed_url.return_value = {
            'signedURL': 'https://storage.invalid/read?token=r'}
        patcher = mock.patch('projects.video_proto.bucket_api', return_value=self.api)
        self.bucket_api = patcher.start()
        self.addCleanup(patcher.stop)

        self.tess = self.make_user('tess', 'Site Engineer')       # named tester
        self.otto = self.make_user('otto', 'Site Engineer')       # not a tester
        self.admin = self.make_user('ada', 'Admin')
        self.sysadmin = self.make_user('sam', 'System Admin')

    @staticmethod
    def make_user(username, role):
        user = User.objects.create_user(username, password='pw')
        user.profile.role = role       # the profile comes from the post_save signal
        user.profile.save()
        return user

    @staticmethod
    def client_for(user=None, csrf=False):
        client = Client(SERVER_NAME='localhost', enforce_csrf_checks=csrf)
        if user is not None:
            client.force_login(user)
        return client

    @staticmethod
    def post_json(client, name, body):
        return client.post(reverse(name), data=json.dumps(body),
                           content_type='application/json')

    def log(self, client, **body):
        base = {'outcome': 'ok', 'source': 'rec', 'mime': 'video/mp4;codecs=avc1,mp4a.40.2',
                'width': 640, 'height': 480, 'fps': 24, 'duration': 60.2,
                'bytes': 7_400_000, 'upload_seconds': 48.3, 'path': TESS_PATH}
        base.update(body)
        return self.post_json(client, 'video_proto_log', base)


# ---------------------------------------------------------------------------
# V0-1 — the flag
# ---------------------------------------------------------------------------

@override_settings(VIDEO_PROTO_ENABLED=False, VIDEO_PROTO_TESTERS=['tess'],
                   VIDEO_PROTO_BUCKET='video-proto-test')
class FlagOffTests(StorageMockedBase):

    def test_every_route_is_404_for_everyone_with_the_flag_off(self):
        for who in (None, self.tess, self.admin):
            client = self.client_for(who)
            for name in GET_ROUTES + POST_ROUTES:
                for method in (client.get, client.post):
                    with self.subTest(who=who and who.username, route=name, method=method.__name__):
                        response = method(reverse(name) + f'?path={TESS_PATH}')
                        self.assertEqual(response.status_code, 404)
        self.api.create_signed_upload_url.assert_not_called()
        self.api.create_signed_url.assert_not_called()
        self.assertFalse(proto_logs().exists())

    def test_the_settings_default_to_off_and_empty(self):
        """Read from settings.py's source, not the loaded value, which a local .env could set."""
        source = (Path(video_proto.__file__).resolve().parent.parent
                  / 'solarpms' / 'settings.py').read_text(encoding='utf-8')
        self.assertIn("VIDEO_PROTO_ENABLED = config('VIDEO_PROTO_ENABLED', default=False, cast=bool)", source)
        self.assertIn("VIDEO_PROTO_TESTERS = config('VIDEO_PROTO_TESTERS', default='', cast=Csv())", source)
        self.assertIn("VIDEO_PROTO_BUCKET  = config('VIDEO_PROTO_BUCKET', default='')", source)


# ---------------------------------------------------------------------------
# V0-1, Q1, Q2 — who gets in
# ---------------------------------------------------------------------------

@override_settings(**FLAG_ON)
class AccessTests(StorageMockedBase):

    def test_anonymous_is_sent_to_login(self):
        response = self.client_for().get(reverse('video_proto_page'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/login/')

    def test_a_non_tester_gets_403_everywhere(self):
        for user in (self.otto, self.sysadmin):     # Q2: System Admin is not Admin
            client = self.client_for(user)
            for name in GET_ROUTES:
                with self.subTest(user=user.username, route=name):
                    response = client.get(reverse(name) + f'?path={TESS_PATH}')
                    self.assertEqual(response.status_code, 403)
            for name in POST_ROUTES:
                with self.subTest(user=user.username, route=name):
                    response = self.post_json(client, name, {'path': f'video-proto/{user.username}/{HEX}.mp4',
                                                             'content_type': 'video/mp4', 'bytes': 10,
                                                             'outcome': 'ok', 'source': 'rec'})
                    self.assertEqual(response.status_code, 403)
        self.api.create_signed_upload_url.assert_not_called()
        self.api.create_signed_url.assert_not_called()
        self.assertFalse(proto_logs().exists())

    def test_a_named_tester_gets_the_page_and_the_results(self):
        client = self.client_for(self.tess)
        page = client.get(reverse('video_proto_page'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'Record (640×480, 1 Mbps)')
        self.assertContains(page, 'Record 720p (2 Mbps)')
        self.assertContains(page, 'accept="video/*"')
        self.assertContains(page, reverse('video_proto_sign_upload'))
        self.assertContains(page, 'video/mp4;codecs=avc1,mp4a.40.2')
        self.assertNotContains(page, 'storage is not configured')
        self.assertEqual(client.get(reverse('video_proto_results')).status_code, 200)

    def test_the_admin_role_gets_in_without_being_listed(self):
        client = self.client_for(self.admin)
        self.assertEqual(client.get(reverse('video_proto_page')).status_code, 200)
        self.assertEqual(client.get(reverse('video_proto_results')).status_code, 200)

    @override_settings(VIDEO_PROTO_TESTERS=[' tess ', 'otto'])
    def test_the_tester_list_is_read_at_request_time_and_trimmed(self):
        self.assertEqual(self.client_for(self.otto).get(reverse('video_proto_page')).status_code, 200)
        self.assertEqual(self.client_for(self.tess).get(reverse('video_proto_page')).status_code, 200)

    def test_the_json_endpoints_are_post_only(self):
        client = self.client_for(self.tess)
        for name in POST_ROUTES:
            with self.subTest(route=name):
                self.assertEqual(client.get(reverse(name)).status_code, 405)

    def test_the_page_says_when_storage_is_not_configured(self):
        self.bucket_api.return_value = None
        page = self.client_for(self.tess).get(reverse('video_proto_page'))
        self.assertContains(page, 'Video storage is not configured')


class NoMenuLinkTests(SimpleTestCase):

    def test_no_template_outside_the_prototype_links_to_it(self):
        """V0-1: no menu link. Only the prototype's own templates name its routes."""
        root = Path(video_proto.__file__).resolve().parent / 'templates'
        hits = [str(path.relative_to(root)) for path in root.rglob('*.html')
                if 'video_proto' not in path.parts
                and re.search(r'video[_-]proto', path.read_text(encoding='utf-8', errors='ignore'))]
        self.assertEqual(hits, [])


# ---------------------------------------------------------------------------
# V0-5 — the signed upload URL
# ---------------------------------------------------------------------------

@override_settings(**FLAG_ON)
class SignUploadTests(StorageMockedBase):

    def sign(self, user=None, **body):
        payload = {'content_type': 'video/mp4', 'bytes': 1000}
        payload.update(body)
        return self.post_json(self.client_for(user or self.tess), 'video_proto_sign_upload', payload)

    def test_the_server_picks_a_path_in_the_callers_own_folder(self):
        response = self.sign(path=OTHER_PATH)       # a client-sent path is ignored
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertRegex(data['path'], r'^video-proto/tess/[0-9a-f]{32}\.mp4$')
        self.assertEqual(data['signed_url'], 'https://storage.invalid/upload?token=t')
        self.api.create_signed_upload_url.assert_called_once_with(data['path'])

    def test_each_accepted_type_gets_its_extension(self):
        for content_type, ext in (('video/webm;codecs=vp8,opus', 'webm'),
                                  ('video/quicktime', 'mov'), ('video/3gpp', '3gp'),
                                  ('VIDEO/MP4', 'mp4')):
            with self.subTest(content_type=content_type):
                response = self.sign(content_type=content_type)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.json()['path'].endswith('.' + ext))

    def test_other_types_and_sizes_are_refused_before_signing(self):
        for body in ({'content_type': 'video/x-matroska'}, {'content_type': 'image/jpeg'},
                     {'content_type': ''}, {'bytes': 0}, {'bytes': 'lots'},
                     {'bytes': video_proto.MAX_BYTES + 1}):
            with self.subTest(body=body):
                self.assertEqual(self.sign(**body).status_code, 400)
        self.assertEqual(self.sign(bytes=video_proto.MAX_BYTES).status_code, 200)
        self.assertEqual(self.api.create_signed_upload_url.call_count, 1)

    def test_a_file_upload_is_refused_so_no_video_reaches_the_server(self):
        client = self.client_for(self.tess)
        video = SimpleUploadedFile('clip.mp4', b'\x00' * 1024, content_type='video/mp4')
        response = client.post(reverse('video_proto_sign_upload'),
                               {'file': video, 'content_type': 'video/mp4', 'bytes': 1024})
        self.assertEqual(response.status_code, 400)
        self.api.create_signed_upload_url.assert_not_called()

    def test_storage_not_configured_is_503_and_a_signing_error_is_502(self):
        self.bucket_api.return_value = None
        self.assertEqual(self.sign().status_code, 503)
        self.bucket_api.return_value = self.api
        self.api.create_signed_upload_url.side_effect = RuntimeError('boom')
        with self.assertLogs('projects.video_proto', 'ERROR'):
            self.assertEqual(self.sign().status_code, 502)

    def test_csrf_is_enforced(self):
        client = self.client_for(self.tess, csrf=True)
        response = self.post_json(client, 'video_proto_sign_upload',
                                  {'content_type': 'video/mp4', 'bytes': 10})
        self.assertEqual(response.status_code, 403)
        self.api.create_signed_upload_url.assert_not_called()


# ---------------------------------------------------------------------------
# V0-6 — the playback read URL
# ---------------------------------------------------------------------------

@override_settings(**FLAG_ON)
class SignReadTests(StorageMockedBase):

    def read(self, path, user=None):
        return self.post_json(self.client_for(user or self.tess), 'video_proto_sign_read',
                              {'path': path})

    def test_own_upload_gets_a_300_second_link(self):
        response = self.read(TESS_PATH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['url'], 'https://storage.invalid/read?token=r')
        self.api.create_signed_url.assert_called_once_with(TESS_PATH, 300)

    def test_paths_outside_the_callers_own_folder_are_refused(self):
        for path in (OTHER_PATH, f'video-proto/tess/../otto/{HEX}.mp4',
                     f'video-proto/tess/{HEX}.exe', 'video-proto/tess/not-hex.mp4',
                     f'video-proto/tess/sub/{HEX}.mp4', f'HRP-RES-001/grn-document/{HEX}.mp4',
                     f'/video-proto/tess/{HEX}.mp4', ''):
            with self.subTest(path=path):
                self.assertEqual(self.read(path).status_code, 403)
        # Admin too: the playback check reads only the caller's own upload.
        self.assertEqual(self.read(TESS_PATH, user=self.admin).status_code, 403)
        self.api.create_signed_url.assert_not_called()


# ---------------------------------------------------------------------------
# V0-2, Q3 — one log line per try
# ---------------------------------------------------------------------------

@override_settings(**FLAG_ON)
class LogTests(StorageMockedBase):

    def test_a_success_writes_one_line_with_the_numbers(self):
        response = self.log(self.client_for(self.tess))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'logged': True})
        row = proto_logs().get()
        self.assertEqual(row.action_code, 'video_proto_upload')
        self.assertIsNone(row.project)
        self.assertEqual(row.actor, self.tess.profile)
        self.assertEqual(row.entity_type, 'VideoProto')
        self.assertEqual(
            row.action,
            'Video test ok: src=rec mime=video/mp4;codecs=avc1,mp4a.40.2 '
            f'res=640x480 fps=24 dur=60.2 bytes=7400000 up=48.30 file={HEX}.mp4')

    def test_the_stored_picture_size_is_logged_and_shown(self):
        """played=WxH is the stored file's own size at playback: it proves a video track
        and shows orientation (a portrait phone can store 480x640 from a 640x480 camera)."""
        self.log(self.client_for(self.tess), played_width=480, played_height=640)
        line = proto_logs().get().action
        self.assertIn(f'played=480x640 file={HEX}.mp4', line)
        rows, _, _ = video_proto.results_rows()
        self.assertEqual(rows[0]['played'], '480x640')
        page = self.client_for(self.admin).get(reverse('video_proto_results'))
        self.assertContains(page, '<td>480x640</td>', html=False)

    def test_a_full_failure_line_keeps_room_for_the_error(self):
        """Every number present, the longest recorder format and a 9-letter tester: the
        error still keeps at least 40 characters of the 255."""
        error = 'HTTP 413 {"statusCode":"413","error":"Payload too large","message":"The object exceeded the maximum allowed size"}'
        text, _ = video_proto.build_log_text({
            'outcome': 'failed', 'stage': 'playback_failed', 'source': 'rec720',
            'mime': 'video/mp4;codecs=avc1,mp4a.40.2', 'width': 1280, 'height': 720, 'fps': 30,
            'duration': 90.0, 'bytes': 23_456_789, 'upload_seconds': 123.45,
            'bytes_sent': 23_456_789, 'elapsed': 130.2, 'played_width': 720, 'played_height': 1280,
            'path': f'video-proto/sudhanshu/{HEX}.webm', 'error': error}, 'sudhanshu')
        before_error = text.index(' err=') + len(' err=')
        self.assertGreaterEqual(255 - before_error, 40, text)

    def test_each_failure_stage_is_logged_with_its_stage(self):
        client = self.client_for(self.tess)
        for stage in video_proto.STAGES:
            with self.subTest(stage=stage):
                response = self.log(client, outcome='failed', stage=stage, path='',
                                    bytes_sent=1_234_567, elapsed=22.04, error='network error')
                self.assertEqual(response.status_code, 200)
        rows = ActivityLog.objects.filter(action_code='video_proto_upload_failed')
        self.assertEqual(rows.count(), len(video_proto.STAGES))
        line = rows.get(action__contains='stage=upload_failed').action
        self.assertTrue(line.startswith('Video test failed: src=rec stage=upload_failed '))
        self.assertIn('sent=1234567 el=22.0 err=network error', line)
        self.assertNotIn('file=', line)

    def test_a_failure_after_upload_keeps_its_path(self):
        self.log(self.client_for(self.tess), outcome='failed', stage='playback_failed',
                 error='media error 4')
        self.assertIn(f'file={HEX}.mp4 err=media error 4', proto_logs().get().action)

    def test_bad_lines_are_refused_and_write_nothing(self):
        client = self.client_for(self.tess)
        for body in ({'outcome': 'maybe'}, {'outcome': 'failed', 'stage': 'gremlins'},
                     {'outcome': 'failed', 'stage': ''}, {'source': 'webcam'},
                     {'path': ''}, {'path': OTHER_PATH},
                     {'outcome': 'failed', 'stage': 'upload_failed', 'path': OTHER_PATH}):
            with self.subTest(body=body):
                self.assertEqual(self.log(client, **body).status_code, 400)
        response = client.post(reverse('video_proto_log'), {'outcome': 'ok'})   # not JSON
        self.assertEqual(response.status_code, 400)
        self.assertFalse(proto_logs().exists())

    def test_a_long_error_is_cut_to_the_column_and_odd_values_are_dropped(self):
        self.log(self.client_for(self.tess), outcome='failed', stage='upload_failed',
                 error='x ' * 400, mime='video/mp4 <script>', width='NaN', duration=-3)
        line = proto_logs().get().action
        self.assertLessEqual(len(line), 255)
        self.assertTrue(line.endswith('…'))
        self.assertIn('mime=unreadable', line)
        self.assertNotIn('res=', line)
        self.assertNotIn('dur=', line)


# ---------------------------------------------------------------------------
# V0-7, Q1, Q3 — the results page and the play link
# ---------------------------------------------------------------------------

@override_settings(**FLAG_ON)
class ResultsTests(StorageMockedBase):

    def setUp(self):
        super().setUp()
        tess = self.client_for(self.tess)
        self.log(tess)                                               # 7.4 MB in 60.2 s
        self.log(tess, source='rec720', bytes=12_000_000, duration=60, upload_seconds=80,
                 path=f'video-proto/tess/{"f" * 32}.webm', mime='video/webm;codecs=vp8,opus')
        self.log(tess, outcome='failed', stage='upload_failed', path='', bytes_sent=2_500_000,
                 elapsed=22, error='network error')
        self.log(tess, outcome='failed', stage='permission_denied', path='', bytes=None,
                 error='NotAllowedError: Permission denied')

    def test_rows_and_counts(self):
        rows, stage_counts, ok_count = video_proto.results_rows()
        self.assertEqual(ok_count, 2)
        self.assertEqual(stage_counts, {'permission_denied': 1, 'recorder_error': 0,
                                        'too_large': 0, 'sign_failed': 0,
                                        'upload_failed': 1, 'playback_failed': 0})
        self.assertEqual(len(rows), 4)
        first_ok = rows[3]                                           # newest first
        self.assertTrue(first_ok['ok'])
        self.assertEqual(first_ok['tester'], 'tess')
        self.assertEqual(first_ok['res'], '640x480')
        self.assertAlmostEqual(first_ok['mb'], 7.4)
        self.assertAlmostEqual(first_ok['mb_per_min'], 7.4 / (60.2 / 60))
        self.assertAlmostEqual(first_ok['mbps'], 7.4 * 8 / 48.3)
        self.assertFalse(first_ok['over_budget'])
        self.assertTrue(rows[2]['over_budget'])                      # 12 MB per minute
        self.assertEqual(rows[1]['stage'], 'upload_failed')
        self.assertAlmostEqual(rows[1]['sent_mb'], 2.5)
        self.assertEqual(rows[1]['path'], '')
        self.assertEqual(rows[0]['error'], 'NotAllowedError: Permission denied')

    def test_the_page_shows_successes_failures_counts_and_play_links(self):
        for user in (self.admin, self.tess):
            with self.subTest(user=user.username):
                page = self.client_for(user).get(reverse('video_proto_results'))
                self.assertEqual(page.status_code, 200)
                self.assertContains(page, 'Uploaded and played back: 2')
                self.assertContains(page, 'Failed: 2')
                self.assertContains(page, 'upload_failed: 1')
                self.assertContains(page, 'permission_denied: 1')
                self.assertContains(page, 'too_large: 0')
                self.assertContains(page, f'{reverse("video_proto_play")}?path=video-proto/tess/{HEX}.mp4')
                self.assertContains(page, '>Play</a>', count=2)
                self.assertContains(page, 'NotAllowedError: Permission denied')
        self.api.create_signed_url.assert_not_called()           # links sign at the click

    def test_play_signs_any_testers_video_at_the_click(self):
        response = self.client_for(self.admin).get(reverse('video_proto_play'), {'path': TESS_PATH})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://storage.invalid/read?token=r')
        self.api.create_signed_url.assert_called_once_with(TESS_PATH, 300)

    def test_play_refuses_anything_but_a_prototype_path(self):
        client = self.client_for(self.admin)
        for path in ('', f'HRP-RES-001/grn-document/{HEX}.jpg', f'video-proto/tess/../x/{HEX}.mp4'):
            with self.subTest(path=path):
                self.assertEqual(client.get(reverse('video_proto_play'), {'path': path}).status_code, 404)
        self.api.create_signed_url.assert_not_called()

    def test_play_with_storage_trouble_goes_back_to_the_results(self):
        self.api.create_signed_url.side_effect = RuntimeError('boom')
        with self.assertLogs('projects.video_proto', 'ERROR'):
            response = self.client_for(self.admin).get(reverse('video_proto_play'), {'path': TESS_PATH})
        self.assertRedirects(response, reverse('video_proto_results'), fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# V0-2 — never another bucket
# ---------------------------------------------------------------------------

class BucketNameTests(SimpleTestCase):

    @override_settings(VIDEO_PROTO_BUCKET='video-proto-test', SUPABASE_DELIVERY_BUCKET='Horizon-PMS-Delivery')
    def test_a_separate_bucket_is_used(self):
        self.assertEqual(video_proto._bucket_name(), 'video-proto-test')

    def test_unset_or_shared_buckets_are_refused(self):
        for name, others in (('', {}), ('  ', {}),
                             ('Horizon-PMS-Delivery', {'SUPABASE_DELIVERY_BUCKET': 'Horizon-PMS-Delivery'}),
                             ('solarpms-files', {'SUPABASE_BUCKET': 'solarpms-files'}),
                             ('Horizon-PMS-Bills', {'SUPABASE_BILLS_BUCKET': 'Horizon-PMS-Bills'}),
                             ('Horizon-PMS-Design', {'SUPABASE_DESIGN_BUCKET': 'Horizon-PMS-Design'})):
            with self.subTest(name=name), override_settings(VIDEO_PROTO_BUCKET=name, **others):
                self.assertEqual(video_proto._bucket_name(), '')
                self.assertIsNone(video_proto.bucket_api())

    @override_settings(VIDEO_PROTO_BUCKET='video-proto-test', SUPABASE_URL='', SUPABASE_KEY='')
    def test_no_client_without_a_url_and_key(self):
        with mock.patch('supabase.create_client') as create_client:
            self.assertIsNone(video_proto.bucket_api())
        create_client.assert_not_called()


# ---------------------------------------------------------------------------
# V0-8 — the cleanup command
# ---------------------------------------------------------------------------

def _fake_api(files):
    """A bucket API whose list() serves `files` (paths under video-proto/) one folder at a
    time, as Supabase does, and whose remove() drops them."""
    api = mock.MagicMock()
    store = set(files)

    def list_(folder, options=None):
        prefix = folder + '/'
        names = {}
        for path in store:
            if path.startswith(prefix):
                head, _, tail = path[len(prefix):].partition('/')
                names[head] = (None if tail else 'id')
        return [{'name': n, 'id': i, 'metadata': {'size': 1000} if i else None}
                for n, i in sorted(names.items())]

    def remove(paths):
        store.difference_update(paths)
        return [{'name': p} for p in paths]

    api.list.side_effect = list_
    api.remove.side_effect = remove
    return api, store


@override_settings(**FLAG_ON)
class CleanupCommandTests(StorageMockedBase):

    FILES = (TESS_PATH, f'video-proto/tess/{"a" * 32}.webm', OTHER_PATH)

    def run_command(self, *args):
        out = StringIO()
        call_command('delete_video_proto', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_lists_and_deletes_nothing(self):
        api, store = _fake_api(self.FILES)
        self.bucket_api.return_value = api
        out = self.run_command()
        for path in self.FILES:
            self.assertIn(path, out)
        self.assertIn('3 object(s), 0.0 MB, under video-proto/ in bucket video-proto-test.', out)
        self.assertIn('Dry run: nothing deleted.', out)
        api.remove.assert_not_called()
        self.assertEqual(store, set(self.FILES))

    def test_apply_deletes_everything_under_the_prefix_and_lists_again(self):
        api, store = _fake_api(self.FILES)
        self.bucket_api.return_value = api
        out = self.run_command('--apply')
        api.remove.assert_called_once()
        self.assertEqual(sorted(api.remove.call_args.args[0]), sorted(self.FILES))
        self.assertEqual(store, set())
        self.assertIn('Deleted 3 object(s). Left under video-proto/: 0.', out)

    def test_it_works_with_the_flag_off(self):
        api, _ = _fake_api(self.FILES)
        self.bucket_api.return_value = api
        with override_settings(VIDEO_PROTO_ENABLED=False):
            self.assertIn('3 object(s)', self.run_command())

    def test_it_refuses_without_a_separate_bucket(self):
        for settings_ in ({'VIDEO_PROTO_BUCKET': ''},
                          {'VIDEO_PROTO_BUCKET': 'Horizon-PMS-Delivery',
                           'SUPABASE_DELIVERY_BUCKET': 'Horizon-PMS-Delivery'}):
            with self.subTest(settings=settings_), override_settings(**settings_):
                with self.assertRaisesMessage(CommandError, 'VIDEO_PROTO_BUCKET is unset'):
                    self.run_command('--apply')
        self.api.remove.assert_not_called()
        self.api.list.assert_not_called()
