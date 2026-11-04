import os
import tempfile
import unittest
from pathlib import Path

from real_estate_desktop.config import Settings, load_settings, validate_endpoint


class Endpoint(unittest.TestCase):
    def test_known_providers_are_accepted(self):
        for url in ('https://open.bigmodel.cn/api/paas/v4/',
                    'https://api.deepseek.com/v1',
                    'https://api.openai.com/v1'):
            with self.subTest(url=url):
                self.assertEqual(validate_endpoint(url), url)

    def test_loopback_serves_local_models_over_http(self):
        for url in ('http://127.0.0.1:11434/v1', 'http://localhost:8000/v1'):
            with self.subTest(url=url):
                self.assertEqual(validate_endpoint(url), url)

    def test_plain_http_to_the_internet_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_endpoint('http://api.deepseek.com/v1')

    def test_unknown_host_is_rejected_without_opt_in(self):
        with self.assertRaises(ValueError):
            validate_endpoint('https://example.com/v1')

    def test_unknown_host_is_accepted_with_opt_in(self):
        self.assertEqual(validate_endpoint('https://example.com/v1', allow_custom=True),
                         'https://example.com/v1')

    def test_opt_in_can_come_from_the_environment(self):
        os.environ['ALLOW_CUSTOM_BASE_URL'] = '1'
        try:
            self.assertEqual(validate_endpoint('https://llm.internal/v1'), 'https://llm.internal/v1')
        finally:
            os.environ.pop('ALLOW_CUSTOM_BASE_URL', None)

    def test_credentials_in_the_url_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_endpoint('https://key:secret@api.deepseek.com/v1', allow_custom=True)
        with self.assertRaises(ValueError):
            validate_endpoint('https://api.deepseek.com/v1?token=x', allow_custom=True)


class SettingsDefaults(unittest.TestCase):
    def test_defaults_point_at_the_shipped_stack(self):
        settings = Settings(Path('.'))
        self.assertEqual(settings.chat_base_url, 'https://open.bigmodel.cn/api/paas/v4/')
        self.assertEqual(settings.history_turns, 8)
        self.assertEqual((settings.dense_top_k, settings.lexical_top_k, settings.rrf_k), (10, 10, 60))
        self.assertTrue(settings.embedding_model.endswith('bge-small-zh-v1.5'))

    def test_openai_compatible_base_url_reaches_settings(self):
        os.environ['OPENAI_BASE_URL'] = 'https://api.deepseek.com/v1'
        os.environ['PROPERTY_MODEL'] = 'deepseek-chat'
        try:
            with tempfile.TemporaryDirectory() as folder:
                settings = load_settings(Path(folder))
            self.assertEqual(settings.chat_base_url, 'https://api.deepseek.com/v1')
            self.assertEqual(settings.chat_model, 'deepseek-chat')
        finally:
            os.environ.pop('OPENAI_BASE_URL', None)
            os.environ.pop('PROPERTY_MODEL', None)


if __name__ == '__main__':
    unittest.main()


