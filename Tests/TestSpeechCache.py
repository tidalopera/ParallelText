from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import threading
import time
import unittest
import wave
import Reader as r
from SpeechCache import DiskCache, FurinaClient


class CacheTests(unittest.TestCase):
    def test_evicted_audio_is_regenerated_instead_of_reusing_stale_future(self):
        with TemporaryDirectory() as directory:
            store=r.CachedSpeechStore([{"english":"Test words."}],Path(directory))
            def fake(text,path,background):
                with wave.open(str(path),"wb") as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(32000);audio.writeframes(b"\0\1"*3200)
            try:
                with patch.object(store.client,"generate",side_effect=fake) as generate:
                    key=store.key(0,"furina-local-en")
                    store.request(0,"furina-local-en");store.entries[key].result(timeout=3)
                    (Path(directory)/(key+".json")).unlink()
                    (Path(directory)/(key+".wav")).unlink()
                    store.request(0,"furina-local-en");store.entries[key].result(timeout=3)
                    self.assertEqual(generate.call_count,2)
                    self.assertGreater(len(store.audio(key)),44)
            finally:store.close()

    def test_prefetch_plan_respects_cache_budget(self):
        with TemporaryDirectory() as directory:
            store=r.CachedSpeechStore([{"english":"Words"} for _ in range(3)],Path(directory),{"speechCacheBytes":10000})
            try:
                result=store.prefetch_plan(0,"en-US-AriaNeural",[{"index":1,"voice":"en-US-AriaNeural"}])
                self.assertEqual(result["indices"],[])
                self.assertTrue(store.prefetch_status()["cacheBudgetLimited"])
                self.assertLessEqual(store.cache.stats()["bytes"],10000)
            finally:store.close()

    def test_quota_and_restart(self):
        with TemporaryDirectory() as directory:
            cache = DiskCache(directory, 1500)
            for i in range(4):
                key = f"{i:024x}"
                (Path(directory)/(key+".wav")).write_bytes(b"a"*500)
                cache.put(key, {"format":"wav", "index":i, "book":"test"}, key)
            self.assertLessEqual(cache.stats()["bytes"],1500)
            self.assertIsNone(cache.get(f"{0:024x}"))
            self.assertIsNotNone(DiskCache(directory,1500).get(f"{3:024x}"))

    def test_splitting_preserves_all_non_whitespace_characters(self):
        text = ("A long sentence, with details. Another sentence! "*40)
        chunks = FurinaClient.split(text)
        self.assertTrue(all(len(chunk)<=450 for chunk in chunks))
        self.assertEqual("".join("".join(chunks).split()), "".join(text.split()))

    def test_audio_reused_after_store_restart(self):
        book = [{"index":0,"english":"Test words.","chinese":"Source","chapter":"Test"}]
        with TemporaryDirectory() as directory:
            def fake(text,path,background):
                with wave.open(str(path),"wb") as audio:
                    audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(32000)
                    audio.writeframes(b"\0\1"*3200)
            store = r.CachedSpeechStore(book,Path(directory))
            try:
                with patch.object(store.client,"generate",side_effect=fake):
                    result=store.request(0,"furina-local-en")
                    key=store.key(0,"furina-local-en")
                    store.entries[key].result(timeout=3)
                    self.assertEqual(store.status(key)["state"],"ready")
            finally: store.close()
            changed_book=book+[{'index':1,'english':'An unrelated revision.','chinese':'Another','chapter':'Test'}]
            second=r.CachedSpeechStore(changed_book,Path(directory))
            try:
                with patch.object(second.client,"generate",side_effect=AssertionError("Should reuse disk")):
                    result=second.request(0,"furina-local-en")
                    self.assertEqual(result["state"],"ready")
                    self.assertEqual(result["words"],[])
                    self.assertTrue(result["audio"].endswith(".wav"))
                    self.assertGreater(len(second.audio(key)),44)
            finally: second.close()

    def test_busy_resources_pause_preparation(self):
        with TemporaryDirectory() as directory:
            store=r.CachedSpeechStore([{"english":"Words"}],Path(directory))
            try:
                with patch.object(store.monitor,"sample",return_value={"busy":True}), patch.object(store,"request") as request:
                    store.start_preparation(0,"furina-local-en")
                    deadline=time.monotonic()+2
                    while store.preparation["state"]!="paused-resources" and time.monotonic()<deadline:
                        time.sleep(.02)
                    self.assertEqual(store.preparation["state"],"paused-resources")
                    request.assert_not_called()
            finally:
                store.close()
                store.preparing.join(timeout=2)

    def test_repair_queue_rebuilds_audio_and_serves_it_ready(self):
        book=[{'index':i,'english':text,'chinese':'Source','chapter':'Test'} for i,text in enumerate(['First words.','Second words.'])]
        with TemporaryDirectory() as directory:
            store=r.CachedSpeechStore(book,Path(directory))
            def generate(text,path,background):
                with wave.open(str(path),'wb') as audio:
                    audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(32000);audio.writeframes(b'\0\1'*100)
            try:
                with patch.object(store.client,'generate',side_effect=generate):
                    store.start_repairs([{'index':i,'voice':'furina-local-en'} for i in range(2)])
                    deadline=time.monotonic()+3
                    while store.repairs['state']=='regenerating' and time.monotonic()<deadline:time.sleep(.02)
                    self.assertEqual(store.repairs['state'],'complete')
                    self.assertEqual(store.repairs['done'],2)
                    self.assertEqual(store.repairs['errors'],[])
                    for i in range(2):self.assertEqual(store.request(i,'furina-local-en')['state'],'ready')
            finally:store.close()


if __name__=="__main__": unittest.main()
