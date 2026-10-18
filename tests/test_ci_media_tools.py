"""CI dependency policy with fake processes; never installs host packages."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import ci_media_tools as media
from tools import ci_test_shards


class MediaToolTests(unittest.TestCase):
    def test_https_preserves_official_mirror_order_and_signed_by(self):
        text = ("http://azure.archive.ubuntu.com/ubuntu priority:10\n"
                "http://archive.ubuntu.com/ubuntu priority:20\n"
                "URIs: http://security.ubuntu.com/ubuntu\n"
                "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
                "https://unrelated.example/unchanged\n")
        result = media.https_sources(text)
        self.assertEqual(result, text.replace("http://", "https://"))
        self.assertLess(result.index("azure.archive"), result.index("https://archive"))

    def test_available_tools_do_not_update_or_install(self):
        with patch.object(media, "missing_packages", return_value=[]), patch.object(media, "run") as run:
            media.install("unused")
        run.assert_not_called()

    def test_unreachable_official_mirror_removed_once_without_new_mirror(self):
        path = "/etc/apt/apt-mirrors.txt"
        sources = {path: "http://azure.archive.ubuntu.com/ubuntu priority:10\n"
                        "http://archive.ubuntu.com/ubuntu priority:20\n",
            "/etc/apt/sources.list.d/ubuntu.sources":
                "URIs: mirror+file:/etc/apt/apt-mirrors.txt\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"}
        probes = []
        def probe(host):
            probes.append(host)
            return host == "archive.ubuntu.com"
        result = media.reachable_sources(sources, probe)
        self.assertEqual(result[path], "https://archive.ubuntu.com/ubuntu priority:20\n")
        self.assertEqual(result["/etc/apt/sources.list.d/ubuntu.sources"], sources["/etc/apt/sources.list.d/ubuntu.sources"])
        self.assertEqual(sorted(probes), ["archive.ubuntu.com", "azure.archive.ubuntu.com"])
        with self.assertRaisesRegex(RuntimeError, "No configured"):
            media.reachable_sources(sources, lambda host: False)

    def test_unreachable_direct_source_cannot_acquire_a_guessed_fallback(self):
        with self.assertRaisesRegex(RuntimeError, "direct Ubuntu"):
            media.reachable_sources({"/etc/apt/sources.list.d/ubuntu.sources":
                "URIs: http://azure.archive.ubuntu.com/ubuntu\n"}, lambda host: False)

    def test_mirror_probe_does_not_follow_redirects_and_closes(self):
        with patch.object(media.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 302
            self.assertFalse(media.mirror_available("archive.ubuntu.com"))
            connection.return_value.request.assert_called_once_with("HEAD", "/ubuntu/dists/noble/InRelease")
            connection.return_value.close.assert_called_once()

    def test_missing_font_preserves_usable_ffmpeg(self):
        with patch.object(media, "usable", return_value=True), patch.object(media, "run",
                return_value=subprocess.CompletedProcess([], 0, "DejaVu Sans")):
            self.assertEqual(media.missing_packages(), ["fonts-noto-cjk"])

    def test_install_restored_archives_only_through_signed_apt_and_verify(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RUNNER_TEMP": temp}), \
                patch.object(media, "SOURCES", ()), patch.object(media, "missing_packages",
                    side_effect=[["ffmpeg", "fonts-noto-cjk"], []]), patch.object(media, "run") as run:
            cache = Path(temp)/"sixnine-ci-apt"
            media.install(cache)
        calls = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "update")
        self.assertEqual(calls[1][-5:], ["install", "-y", "--no-install-recommends", "ffmpeg", "fonts-noto-cjk"])
        self.assertIn("Dir::Cache::archives="+str(cache.resolve()), calls[1])
        self.assertIn("APT::Update::Error-Mode=any", calls[0])
        self.assertFalse(any("allow-unauthenticated" in arg or "dpkg" in arg or arg.endswith(".deb")
                             for call in calls for arg in call))

    def test_install_failure_and_postcheck_failure_do_not_continue(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RUNNER_TEMP": temp}), \
                patch.object(media, "SOURCES", ()), patch.object(media, "missing_packages", return_value=["ffmpeg"]):
            with patch.object(media, "run", side_effect=subprocess.CalledProcessError(1, "apt")), \
                    self.assertRaises(subprocess.CalledProcessError):
                media.install(Path(temp)/"sixnine-ci-apt")
            with patch.object(media, "run"), self.assertRaisesRegex(RuntimeError, "requirements"):
                media.install(Path(temp)/"sixnine-ci-apt")

    def test_workflow_shares_helper_without_removing_gate_commands(self):
        source = (Path(__file__).resolve().parents[1]/".github/workflows/ci.yml").read_text()
        self.assertEqual(source.count("python tools/ci_media_tools.py cache-key"), 2)
        self.assertEqual(source.count("python tools/ci_media_tools.py install"), 2)
        self.assertEqual(source.count("Install media test tools from signed Ubuntu repositories\n        timeout-minutes: 10"), 2)
        postgres = source.split("\n  postgres:\n", 1)[1].split("\n  frontend:\n", 1)[0]
        required = {"test_platform_quick_chat_integration", "test_platform_quick_chat_admission",
                    "test_platform_quick_chat_wangp", "test_platform_agent_connect",
                    "test_platform_agent_connect_app", "test_platform_pool_members",
                    "test_platform_wangp_first_last_policy"}
        self.assertTrue(required <= set(ci_test_shards.POSTGRES_MODULES),
                        "PostgreSQL must retain the Quick Chat, connection and pool/first-last contract checks")
        self.assertIn("python tools/ci_test_shards.py --suite postgres", postgres)
        self.assertIn("python tools/ci_test_shards.py --suite python", source)


if __name__ == "__main__":
    unittest.main()
