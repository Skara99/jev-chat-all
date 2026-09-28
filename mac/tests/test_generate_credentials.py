"""Credential resolution: a lone OPENAI_API_KEY must not hijack the builtin relay.

Regression for the empty-candidate panel: a shell leftover OPENAI_API_KEY
without OPENAI_BASE_URL used to pair glm-4-flash with api.openai.com
(timeout / Connection refused). Run:

    python -B -m unittest tests.test_generate_credentials -v
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import builtin  # noqa: E402
import generate  # noqa: E402


class CredentialTests(unittest.TestCase):
    def test_key_without_base_falls_back_to_builtin(self):
        env = {"OPENAI_API_KEY": "sk-from-shell-rc"}
        with patch.dict(os.environ, env, clear=False):
            # also hide any user env file so the test is hermetic
            with patch("userconfig._merged_env_file", return_value={}):
                with patch("userconfig.parse_env_file", return_value={}):
                    base, key, model, source, api = generate.load_credentials()
        self.assertEqual(base, builtin.BASE_URL)
        self.assertEqual(key, builtin.API_KEY)
        self.assertEqual(source, generate.BUILTIN_SOURCE)
        self.assertEqual(api, "openai")
        self.assertTrue(model)

    def test_key_and_base_together_win(self):
        env = {
            "OPENAI_API_KEY": "sk-mine",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_MODEL": "deepseek-chat",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("userconfig._merged_env_file", return_value={}):
                with patch("userconfig.parse_env_file", return_value={}):
                    base, key, model, source, api = generate.load_credentials()
        self.assertEqual(base, "https://api.deepseek.com")
        self.assertEqual(key, "sk-mine")
        self.assertEqual(model, "deepseek-chat")
        self.assertEqual(api, "openai")
        self.assertNotEqual(source, generate.BUILTIN_SOURCE)

    def test_glm_key_uses_official_zhipu_endpoint(self):
        env = {
            "OPENAI_API_KEY": "sk-from-shell-rc",  # incomplete, must not win
            "GLM_KEY": "zhipu.secret",
            "OPENAI_MODEL": "glm-4-flash",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("userconfig._merged_env_file", return_value={}):
                with patch("userconfig.parse_env_file", return_value={}):
                    base, key, model, source, api = generate.load_credentials()
        self.assertEqual(base, "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(key, "zhipu.secret")
        self.assertEqual(model, "glm-4-flash")
        self.assertEqual(api, "openai")
        self.assertNotEqual(source, generate.BUILTIN_SOURCE)

    def test_complete_openai_pair_still_beats_glm_key(self):
        env = {
            "OPENAI_API_KEY": "sk-mine",
            "OPENAI_BASE_URL": "https://api.deepseek.com",
            "OPENAI_MODEL": "deepseek-chat",
            "GLM_KEY": "zhipu.secret",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("userconfig._merged_env_file", return_value={}):
                with patch("userconfig.parse_env_file", return_value={}):
                    base, key, model, source, api = generate.load_credentials()
        self.assertEqual(base, "https://api.deepseek.com")
        self.assertEqual(key, "sk-mine")
        self.assertEqual(model, "deepseek-chat")

    def test_anthropic_pair_wins_when_openai_incomplete(self):
        env = {
            "OPENAI_API_KEY": "sk-from-shell-rc",   # no base → ignored
            "ANTHROPIC_API_KEY": "sk-anth",
            "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic",
            "ANTHROPIC_MODEL": "glm-4-flash",
        }
        with patch.dict(os.environ, env, clear=False):
            with patch("userconfig._merged_env_file", return_value={}):
                with patch("userconfig.parse_env_file", return_value={}):
                    base, key, model, source, api = generate.load_credentials()
        self.assertIn("anthropic", base.lower())
        self.assertEqual(key, "sk-anth")
        self.assertEqual(api, "anthropic")
        self.assertEqual(model, "glm-4-flash")


if __name__ == "__main__":
    unittest.main()
