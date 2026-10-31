import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts/moviepilot.py"
spec = importlib.util.spec_from_file_location("moviepilot", MODULE_PATH)
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


def context(site, seeders, episode=None):
    return {
        "meta_info": {"begin_season": 1, "begin_episode": episode, "episode_list": [episode] if episode else []},
        "media_info": {"title": "Example", "media_source": "themoviedb", "media_id": "123"},
        "torrent_info": {"site_name": site, "title": f"Example S01{'E01' if episode else ''} 1080p CATCHPLAY WEB-DL AAC-ADWeb",
                         "size": 2**30, "seeders": seeders, "enclosure": "https://site.invalid/download?passkey=SECRET",
                         "site_cookie": "SECRET", "page_url": "https://site.invalid/details?id=9&token=SECRET"},
    }


class FakeClient:
    base = "http://127.0.0.1:13001/api/v1"

    def __init__(self, tasks=None, fail=False):
        self.calls = []
        self.tasks = tasks or []
        self.fail = fail

    def api(self, path, params=None, payload=None):
        self.calls.append((path, payload))
        if payload is None:
            return {"success": True, "data": self.tasks}
        if self.fail:
            raise mp.MoviePilotError("模拟连接断开")
        return {"success": True, "data": {"download_id": "abc123"}}


class MoviePilotTests(unittest.TestCase):
    def setUp(self):
        home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
        root = Path(os.environ["TMPDIR"]) if os.environ.get("TMPDIR") else home / "cache/scratch"
        root.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(prefix="moviepilot-test-", dir=root)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "snapshot.json"
        self.snapshot = {"version": 1, "created_at": "2026-10-31T00:00:00+00:00", "base_url": FakeClient.base,
                         "contexts": [context("A", 2), context("B", 8), context("C", 99, 1), context("D", None)]}
        mp.private_write(self.path, self.snapshot)

    def test_rank_packs_keeps_snapshot_indices_and_omits_secrets(self):
        rows = mp.ranked(self.snapshot, ["CATCHPLAY", "ADWeb", "1080p"], packs=True)
        self.assertEqual([row["index"] for row in rows], [1, 0, 3])
        self.assertNotIn("SECRET", json.dumps(rows))
        self.assertEqual(mp.ranked(self.snapshot, site="A")[0]["index"], 0)

    def test_connection_requires_explicit_base_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(mp.MoviePilotError, "MOVIEPILOT_BASE_URL"):
                mp.Client()

    def test_auth_does_not_probe_maintainer_credential_path(self):
        with patch.dict(os.environ, {"MOVIEPILOT_BASE_URL": FakeClient.base}, clear=True):
            with patch.object(mp, "env_credentials", side_effect=AssertionError("不应猜测凭据路径")):
                with self.assertRaisesRegex(mp.MoviePilotError, "API 凭据"):
                    mp.Client()

    def test_explicit_env_file_is_read_without_shell_evaluation(self):
        config = Path(self.folder.name) / "app.env"
        config.write_text("export API_TOKEN='literal-$(not-executed)'\n", encoding="utf-8")
        with patch.dict(os.environ, {"MOVIEPILOT_BASE_URL": FakeClient.base,
                                     "MOVIEPILOT_ENV_FILE": str(config)}, clear=True):
            client = mp.Client()
        self.assertEqual(client.key, "literal-$(not-executed)")

    def test_download_preview_performs_no_network_or_receipt_write(self):
        with patch.object(mp, "Client", side_effect=AssertionError("网络不应被调用")):
            result = mp.download(self.path, 1)
        self.assertFalse(result["submitted"])
        self.assertEqual(list(Path(self.folder.name).iterdir()), [self.path])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_submit_uses_exact_selected_context_once(self):
        client = FakeClient()
        result = mp.download(self.path, 1, submit=True, client=client)
        self.assertTrue(result["submitted"])
        self.assertEqual(client.calls[1][1]["torrent_in"], self.snapshot["contexts"][1]["torrent_info"])
        replay = mp.download(self.path, 1, submit=True, client=client)
        self.assertTrue(replay["already_attempted"])
        self.assertEqual(len(client.calls), 2)

    def test_uncertain_submission_does_not_retry(self):
        client = FakeClient(fail=True)
        with self.assertRaises(mp.MoviePilotError):
            mp.download(self.path, 1, submit=True, client=client)
        result = mp.download(self.path, 1, submit=True, client=client)
        self.assertEqual(result["receipt"]["state"], "uncertain")
        self.assertEqual(len(client.calls), 2)

    def test_duplicate_title_with_dots_is_not_submitted(self):
        title = self.snapshot["contexts"][1]["torrent_info"]["title"].replace(" ", ".")
        client = FakeClient(tasks=[{"title": title, "hash": "existing"}])
        result = mp.download(self.path, 1, submit=True, client=client)
        self.assertEqual(result["existing_tasks"][0]["hash"], "existing")
        self.assertEqual(len(client.calls), 1)

    def test_snapshot_and_receipt_permissions(self):
        mp.download(self.path, 1, submit=True, client=FakeClient())
        for path in Path(self.folder.name).iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_different_server_is_rejected(self):
        client = FakeClient()
        client.base = "https://different.invalid/api/v1"
        with self.assertRaises(mp.MoviePilotError):
            mp.download(self.path, 1, submit=True, client=client)
        self.assertFalse(client.calls)

    def test_cli_search_never_posts(self):
        client = FakeClient()
        client.api = lambda path, params=None, payload=None: (
            self.assertIsNone(payload) or {"success": True, "data": self.snapshot["contexts"]}
        )
        with patch.object(mp, "Client", return_value=client), patch.dict(os.environ, {"TMPDIR": self.folder.name}):
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                mp.main(["resources", "--id", "123", "--type", "tv", "--season", "1"])
        output = json.loads(stream.getvalue())
        self.assertEqual(output["count"], 4)
        self.assertNotIn("SECRET", stream.getvalue())
        path = Path(output["snapshot"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
