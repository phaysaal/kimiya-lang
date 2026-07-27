import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kimiya.runtime import Agent, Oracle, _agent_api_key


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}]}'


class CredentialFileTests(unittest.TestCase):
    def test_supported_file_formats(self):
        cases = [
            "secret-token\n",
            "OPENROUTER_API_KEY=secret-token\n",
            '{"api_key":"secret-token"}\n',
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.api"
            for content in cases:
                path.write_text(content, encoding="utf-8")
                agent = Agent("A", "model", backend="openrouter",
                              key_file=str(path))
                self.assertEqual(_agent_api_key(agent), "secret-token")

    def test_zdr_and_bearer_are_sent_but_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.api"
            path.write_text("secret-token\n", encoding="utf-8")
            agent = Agent("A", "google/gemini-2.5-flash",
                          backend="openrouter", key_file=str(path), zdr=True)
            requests = []

            def capture(request, timeout):
                requests.append(request)
                return _Response()

            with patch("urllib.request.urlopen", side_effect=capture):
                result = Oracle().complete(agent, "inspect")

            self.assertEqual(result, "ok")
            payload = json.loads(requests[0].data)
            self.assertEqual(payload["provider"], {"zdr": True})
            self.assertEqual(requests[0].headers["Authorization"],
                             "Bearer secret-token")
            self.assertNotIn("secret-token", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
