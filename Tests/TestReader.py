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
    def test_prefetch_looks_ahead_over_symbols(self):
        book = [{"english": text} for text in ["Now", "...", "One", "Two", "!", "Three", "Four", "Five", "Six", "Seven"]]
        with TemporaryDirectory() as directory:
            store = r.SpeechStore(book, Path(directory))
            try:
                with patch.object(store, "request", return_value={"state": "pending"}) as request:
                    self.assertEqual(store.prefetch(0, "voice")["indices"], [2, 3, 5, 6, 7, 8])
                    self.assertTrue(all(call.kwargs["background"] for call in request.call_args_list))
            finally:
                store.close()

    def test_current_paragraph_promoted_out_of_prefetch_queue(self):
        started = [threading.Event(), threading.Event()]
        release = threading.Event()
        def generate(index, voice, key):
            if index in (0, 1):
                started[index].set()
                release.wait(5)
            return {"index": index, "audio": "/audio/test.mp3", "words": []}
        with TemporaryDirectory() as directory:
            store = r.SpeechStore([{"english": "Words"} for _ in range(5)], Path(directory))
            try:
                with patch.object(store, "_generate", side_effect=generate):
                    store.request(0, "voice", background=True)
                    store.request(1, "voice", background=True)
                    self.assertTrue(all(event.wait(2) for event in started))
                    store.request(2, "voice", background=True)
                    queued = store.entries[store.key(2, "voice")]
                    store.request(2, "voice")
                    self.assertTrue(queued.cancelled())
                    self.assertEqual(store.entries[store.key(2, "voice")].result(timeout=2)["index"], 2)
                    # Both speculative workers are still blocked; foreground was independent.
                    self.assertFalse(release.is_set())
            finally:
                release.set()
                store.close()

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
