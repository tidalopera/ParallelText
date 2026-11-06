import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import Pipeline as p
import Quality as q


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original = p.ROOT, p.SOURCE, p.SETTINGS, dict(p.MODELS)
        self.addCleanup(self.restore)
        root = Path(self.temp.name)
        p.ROOT = root / "Work"
        p.SOURCE = root / "Input.txt"
        p.SETTINGS = {"encoding": "utf-8", "batchChars": 120}
        p.MODELS.update({stage: "test-model" for stage in p.MODELS})
        self.demo = p.read(p.BASE / "Examples" / "Book" / "Aligned.json")
        p.SOURCE.write_bytes((p.BASE / "Examples" / "Source.txt").read_bytes())

    def restore(self):
        p.ROOT, p.SOURCE, p.SETTINGS, models = self.original
        p.MODELS.clear()
        p.MODELS.update(models)

    def prepare_translations(self, wrong=False):
        p.prepare()
        p.save(p.ROOT / "Glossary.json", {"sourcehash": p.read(p.ROOT / "Source.json")["sourcehash"], "terms": []})
        for path in (p.ROOT / "Batches").glob("*.json"):
            items = [{"id": i["id"], "text": "Two notebooks lay on the desk." if wrong and i["id"] == 4 else self.demo[i["id"]]["translation"]} for i in p.read(path)]
            p.save(p.ROOT / "Translations" / path.name, {"items": items})

    def test_source_and_batch_changes_cannot_overwrite_workspace(self):
        p.prepare()
        before = (p.ROOT / "Source.json").read_bytes()
        p.prepare()
        self.assertEqual((p.ROOT / "Source.json").read_bytes(), before)
        p.SETTINGS["batchChars"] = 200
        with self.assertRaisesRegex(ValueError, "different source or batch layout"):
            p.prepare()
        p.SETTINGS["batchChars"] = 120
        p.SOURCE.write_text("Changed text", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different source or batch layout"):
            p.prepare()
        self.assertEqual((p.ROOT / "Source.json").read_bytes(), before)

    def test_review_is_explicit_and_stale_corrections_are_rejected(self):
        self.prepare_translations(wrong=True)
        original = p.SOURCE.read_bytes()
        q.check(all_paragraphs=True)
        with patch.object(p, "invoke", return_value={"corrections": [{"id": 4, "text": self.demo[4]["translation"], "reason": "Source says three notebooks."}]}):
            q.review(1)
        self.assertFalse((p.ROOT / "Corrections.json").exists())
        p.assemble()
        self.assertIn("Two notebooks", p.read(p.ROOT / "Aligned.json")[4]["translation"])
        q.apply()
        p.assemble()
        aligned = p.read(p.ROOT / "Aligned.json")
        self.assertEqual([i["translation"] for i in aligned], [i["translation"] for i in self.demo])
        self.assertEqual(p.SOURCE.read_bytes(), original)
        for path in (p.ROOT / "Translations").glob("*.json"):
            data = p.read(path)
            if any(i["id"] == 4 for i in data["items"]):
                next(i for i in data["items"] if i["id"] == 4)["text"] = "An updated initial translation."
                p.save(path, data)
                break
        with self.assertRaisesRegex(ValueError, "stale"):
            p.assemble()

    def test_rejected_proposal_is_not_applied(self):
        self.prepare_translations(wrong=True)
        q.check(all_paragraphs=True)
        with patch.object(p, "invoke", return_value={"corrections": [{"id": 4, "text": self.demo[4]["translation"], "reason": "Test proposal"}]}):
            q.review(1)
        p.save(p.ROOT / "Reviewselection.json", [{"id": 4, "reason": "Test rejection"}])
        q.apply()
        self.assertEqual(p.read(p.ROOT / "Corrections.json")["items"], [])

    def test_missing_duplicate_and_untranslated_batch_output_rejected(self):
        p.prepare()
        paths = sorted((p.ROOT / "Batches").glob("*.json"))
        allitems = p.read(p.ROOT / "Source.json")["items"]
        for result in ({"items": []}, {"items": [{"id": 0, "text": "Duplicate"}, {"id": 0, "text": "Duplicate"}]},
                       {"items": [{"id": i["id"], "text": i["source"]} for i in p.read(paths[0])]}):
            with self.subTest(result=result), patch.object(p, "invoke", return_value=result):
                with self.assertRaisesRegex(ValueError, "Validation failed"):
                    p.translate_one(paths[0], allitems, [])
        self.assertFalse((p.ROOT / "Translations" / paths[0].name).exists())

    def test_chapter_numbers_and_nonstandard_boundary(self):
        for text, number in [("第一百七十六 标题", 176), ("第十万零一章 标题", 100001), ("Chapter 232: Title", 232)]:
            self.assertTrue(p.CHAPTER.match(text))
            self.assertEqual(q.chapter_number(text), number)

    def test_chapter_contract_survives_glossary_and_review(self):
        self.prepare_translations()
        allitems = p.read(p.ROOT / "Source.json")["items"]
        path = next(path for path in (p.ROOT / "Batches").glob("*.json") if any(i["id"] == 1 for i in p.read(path)))
        result = {"items": [{"id": i["id"], "text": "Chapter One: Morning Notes" if i["id"] == 1 else self.demo[i["id"]]["translation"]} for i in p.read(path)]}
        with patch.object(p, "invoke", return_value=result), self.assertRaisesRegex(ValueError, "Validation failed"):
            p.translate_one(path, allitems, [])
        q.check(all_paragraphs=True)
        with patch.object(p, "invoke", return_value={"corrections": [{"id": 1, "text": "Chapter One: Morning Notes", "reason": "Test invalid suggestion"}]}):
            q.review(1)
        with self.assertRaisesRegex(ValueError, "chapter number"):
            q.apply()

    def test_invoke_cache_includes_endpoint_and_reasoning(self):
        calls = []
        result = {"items": [{"id": 0, "text": "Hello"}]}
        final = SimpleNamespace(output_text=json.dumps(result), id="test-response", usage=None)
        class Stream:
            def __enter__(self):
                return iter([SimpleNamespace(type="response.completed", response=final)])
            def __exit__(self, *_):
                return False
        def create(**kwargs):
            calls.append(kwargs)
            return Stream()
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        with patch.object(p, "OpenAI", return_value=client), patch.object(p, "access_token", return_value="test-token"), patch.dict(p.os.environ, {"OPENAI_BASE_URL": "https://one.example/v1"}):
            self.assertEqual(p.invoke("translate", "Test", "Translate", {}, p.TRANSLATION_SCHEMA), result)
            p.invoke("translate", "Test", "Translate", {}, p.TRANSLATION_SCHEMA)
            self.assertEqual(len(calls), 1)
            p.os.environ["OPENAI_BASE_URL"] = "https://two.example/v1"
            p.invoke("translate", "Test", "Translate", {}, p.TRANSLATION_SCHEMA)
            p.SETTINGS["reasoningEffort"] = "medium"
            p.invoke("translate", "Test", "Translate", {}, p.TRANSLATION_SCHEMA)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("reasoning", calls[0])
        self.assertEqual(calls[-1]["reasoning"], {"effort": "medium"})
        self.assertTrue(all(c["store"] is False and c["stream"] is True for c in calls))


if __name__ == "__main__":
    unittest.main()
