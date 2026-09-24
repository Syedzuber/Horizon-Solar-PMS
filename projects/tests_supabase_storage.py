"""
Both storage clients read SUPABASE_URL / SUPABASE_KEY from Django settings, never from
os.environ. Settings get them through python-decouple (os.environ first, then .env), so
reading settings honours Railway's variables and a local .env alike.

Each test clears both names from os.environ, so a pass proves the value came from
settings. `create_client` is mocked — no network.
"""
import os
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from projects import design_storage
from projects.supabase_storage import get_supabase_client

URL = 'https://settings-only.supabase.co'
KEY = 'settings-only-key'


def _environ_without_supabase():
    return {k: v for k, v in os.environ.items() if k not in ('SUPABASE_URL', 'SUPABASE_KEY')}


class GetSupabaseClientTests(SimpleTestCase):

    @override_settings(SUPABASE_URL=URL, SUPABASE_KEY=KEY)
    def test_client_is_built_from_settings(self):
        with patch.dict(os.environ, _environ_without_supabase(), clear=True), \
                patch('supabase.create_client') as create_client:
            client = get_supabase_client()
        create_client.assert_called_once_with(URL, KEY)
        self.assertIs(client, create_client.return_value)

    def test_empty_setting_raises(self):
        for url, key in ((URL, ''), ('', KEY)):
            with self.subTest(url=url, key=key), \
                    override_settings(SUPABASE_URL=url, SUPABASE_KEY=key), \
                    patch.dict(os.environ, {'SUPABASE_URL': URL, 'SUPABASE_KEY': KEY}), \
                    patch('supabase.create_client') as create_client:
                with self.assertRaisesMessage(
                        ValueError, 'SUPABASE_URL and SUPABASE_KEY must be configured'):
                    get_supabase_client()
                create_client.assert_not_called()


class DesignStorageClientTests(SimpleTestCase):

    @override_settings(SUPABASE_URL=URL, SUPABASE_KEY=KEY)
    def test_client_is_built_from_settings(self):
        with patch.dict(os.environ, _environ_without_supabase(), clear=True), \
                patch('supabase.create_client') as create_client:
            client = design_storage._client()
        create_client.assert_called_once_with(URL, KEY)
        self.assertIs(client, create_client.return_value)

    def test_empty_setting_raises(self):
        for url, key in ((URL, ''), ('', KEY)):
            with self.subTest(url=url, key=key), \
                    override_settings(SUPABASE_URL=url, SUPABASE_KEY=key), \
                    patch.dict(os.environ, {'SUPABASE_URL': URL, 'SUPABASE_KEY': KEY}), \
                    patch('supabase.create_client') as create_client:
                with self.assertRaisesMessage(
                        design_storage.DesignStorageError,
                        'Supabase is not configured (SUPABASE_URL / SUPABASE_KEY).'):
                    design_storage._client()
                create_client.assert_not_called()
