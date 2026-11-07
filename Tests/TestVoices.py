import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import Voices as v
import Reader as r


class VoiceTests(unittest.TestCase):
    def test_catalogue_and_existing_voices(self):
        catalogue = v.load_catalogue()
        self.assertGreater(len(catalogue), 4)
        ids = {voice["id"] for voice in catalogue}
        self.assertEqual(len(ids), len(catalogue))
        self.assertTrue(ids <= r.VOICE_IDS)
        self.assertTrue({"furina-local-en", "en-US-AriaNeural", "en-US-JennyNeural",
                         "en-US-GuyNeural", "en-GB-SoniaNeural"} <= r.VOICE_IDS)
        with TemporaryDirectory() as directory:
            store = r.CachedSpeechStore([{"english":"Words"}], Path(directory), {})
            try:
                self.assertNotEqual(store.key(0,"en-US-AriaNeural"), store.key(0,"en-CA-ClaraNeural"))
            finally:
                store.close()

    def test_corrupt_cache_falls_back_and_failed_refresh_preserves_cache(self):
        with TemporaryDirectory() as directory:
            cache, bundled = Path(directory)/"Cache.json", Path(directory)/"Bundled.json"
            rows = v.normalize_voices([{"ShortName":"en-CA-ClaraNeural", "Locale":"en-CA", "Gender":"Female"}])
            bundled.write_text(json.dumps(rows), encoding="utf-8")
            cache.write_text("broken", encoding="utf-8")
            self.assertEqual(v.load_catalogue(cache,bundled), rows)
            cache.write_text(json.dumps(rows), encoding="utf-8")
            before = cache.read_bytes()
            with patch.object(v.edge_tts,"list_voices",new=AsyncMock(side_effect=OSError("offline"))):
                with self.assertRaises(OSError): asyncio.run(v.refresh_catalogue(cache))
            self.assertEqual(cache.read_bytes(),before)
            with patch.object(v.edge_tts,"list_voices",new=AsyncMock(return_value=[])):
                with self.assertRaises(ValueError): asyncio.run(v.refresh_catalogue(cache))
            self.assertEqual(cache.read_bytes(),before)
