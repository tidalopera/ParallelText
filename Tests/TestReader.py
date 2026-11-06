import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import Reader as r


class ReaderTests(unittest.TestCase):
    def test_alignment_mismatch_is_rejected(self):
        with TemporaryDirectory() as directory:
            book = Path(directory)
            (book / "Aligned.json").write_text(json.dumps([{"translation": "One", "source": "Source", "chapter": "Test"}]), encoding="utf-8")
            (book / "English.txt").write_text("Two\n\n", encoding="utf-8")
            with patch.object(r, "BOOK", book), self.assertRaises(ValueError):
                r.load_book()

    def test_live_http_skips_symbols_and_enforces_origin(self):
        with TemporaryDirectory() as directory:
            book = [{"index": 0, "english": "...", "chinese": "...", "chapter": "Test"}]
            store = r.SpeechStore(book, Path(directory))
            server = r.ThreadingHTTPServer(("127.0.0.1", 0), r.make_handler(book, store))
            port = server.server_address[1]
            with patch.object(r, "PORT", port):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
                        health = json.load(response)
                    self.assertEqual(health["app"], "ParallelText")
                    self.assertEqual(health["bookId"], r.book_id(book))
                    body = json.dumps({"index": 0, "voice": r.VOICES[0]["id"]}).encode()
                    request = Request(f"http://127.0.0.1:{port}/api/speech", data=body, headers={"Content-Type": "application/json"})
                    with urlopen(request, timeout=3) as response:
                        self.assertEqual(json.load(response)["state"], "skipped")
                    self.assertFalse(store.entries)
                    request.add_header("Origin", "https://untrusted.example")
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=3)
                    self.assertEqual(error.exception.code, 403)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)
                    store.close()


if __name__ == "__main__":
    unittest.main()
