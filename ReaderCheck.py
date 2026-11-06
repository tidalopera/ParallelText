"""Verify symbol-only speech handling without contacting the TTS service."""
from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
import Reader as r


def main():
    texts = ["...", "……", "— *** ()", "🙂", "", "0", "Hello…", "café", "测试"]
    assert [r.has_speech(text) for text in texts] == [False]*5 + [True]*4
    book = [{"english": text} for text in texts]
    with TemporaryDirectory() as directory:
        store = r.SpeechStore(book, Path(directory))
        class Pool:
            def __init__(self):
                self.calls = []
            def submit(self, *args):
                self.calls.append(args)
                result = Future()
                result.set_result({"audio": "/audio/test.mp3", "words": []})
                return result
        actual_pool = store.pool
        try:
            pool = Pool()
            store.pool = pool
            for index in range(5):
                assert store.request(index, r.VOICES[0]["id"])["state"] == "skipped"
            assert not pool.calls and not store.entries and not list(Path(directory).iterdir())
            for index in range(5, len(texts)):
                assert store.request(index, r.VOICES[0]["id"])["state"] == "ready"
            assert len(pool.calls) == 4
        finally:
            store.pool = actual_pool
            store.close()
    print("SpeechStore checks passed: symbols bypass TTS/cache; words and numbers are synthesized")


if __name__ == "__main__":
    main()
