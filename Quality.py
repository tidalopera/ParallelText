"""Deterministic consistency checks plus bounded semantic review."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
import argparse
import random
import re
import Pipeline as p

CORRECTION = p.object_schema({"id": {"type": "integer"}, "text": {"type": "string"}, "reason": {"type": "string"}})
SCHEMA = p.object_schema({"corrections": {"type": "array", "items": CORRECTION}})
INSTRUCTIONS = """Check an English translation against its Chinese source and canonical glossary. Review ONLY for substantive errors: omitted/mistranslated meaning, wrong speaker/gender/name/title/entity, numbers, untranslated text, or terminology drift. Preserve accurate English, sentence order, punctuation and tone. Do NOT rewrite for stylistic preference. Do not invent away source inconsistencies or resolve ambiguity without evidence. For each correction return the paragraph id, COMPLETE corrected English paragraph, and a concise reason. Return only corrections, never unchanged paragraphs. Preserve chapter-number duplicates and author notes. Chapter headings must start with Chapter plus the original number in Arabic digits, even if a glossary entry suggests otherwise. Longer glossary terms override shorter ones; grammatical inflections and contextually appropriate short references are allowed. Review ONLY ids in paragraphs, not contextual paragraphs. Return English text without Chinese characters."""


chapter_number = p.chapter_number


def load_items():
    source = p.read(p.ROOT / "Source.json")["items"]
    translations = {}
    for path in sorted((p.ROOT / "Translations").glob("*.json")):
        for item in p.read(path)["items"]:
            if item["id"] in translations:
                raise ValueError("Duplicate translated id")
            translations[item["id"]] = item["text"]
    if set(translations) != {x["id"] for x in source}:
        raise ValueError("Translate all paragraphs before quality review")
    return [{**s, "translation": translations[s["id"]]} for s in source]


def normalize(text):
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def check(all_paragraphs=False):
    items = load_items()
    terms = p.read(p.ROOT / "Glossary.json")["terms"]
    flags = []
    frequencies = Counter()
    for item in items:
        source, target = item["source"], item["translation"]
        reasons = []
        expected_chapter = chapter_number(source)
        if expected_chapter is not None:
            got_chapter = re.search(r"\bChapter\s+(\d+)", target, re.I)
            if not got_chapter or int(got_chapter.group(1)) != expected_chapter:
                reasons.append(f"chapter:{expected_chapter}")
        if len(source) >= 30 and len(target) < len(source)*0.85:
            reasons.append("unusuallyshort")
        if len(source) >= 30 and len(target) > len(source)*7:
            reasons.append("unusuallylong")
        if not p.CHAPTER.match(source):
            source_numbers = set(re.findall(r"\d+(?:\.\d+)?", source))
            target_numbers = set(re.findall(r"\d+(?:\.\d+)?", re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", target)))
            missing = source_numbers-target_numbers
            if missing:
                reasons.append("numbers:"+",".join(sorted(missing)))
        masked = source
        norm = normalize(target)
        mismatches = []
        for term in terms:
            if term["source"] in masked:
                frequencies[term["source"]] += 1
                masked = masked.replace(term["source"], " ")
                if normalize(term["target"]) not in norm:
                    mismatches.append(term["source"])
        if mismatches:
            reasons.append("terms:"+"|".join(mismatches))
        if reasons:
            flags.append({"id": item["id"], "reasons": reasons})
    p.save(p.ROOT / "Qualityflags.json", {"flags": flags, "termOccurrences": dict(frequencies)})
    rng = random.Random(20261031)
    chosen = set(rng.sample(range(len(items)), min(180, len(items))))
    chosen.update(f["id"] for f in flags)
    chosen.update(range(min(12, len(items))))
    chosen.update(range(max(0, len(items)-12), len(items)))
    if all_paragraphs:
        chosen = set(range(len(items)))
    selected = [items[i] for i in sorted(chosen)]
    reviews = p.ROOT / "Reviews"
    if reviews.exists():
        for old in reviews.glob("[0-9][0-9][0-9][0-9].json"):
            old.unlink()
    for offset in range(0, len(selected), 24):
        group = selected[offset:offset+24]
        text = "\n".join(i["source"] for i in group)
        relevant = [t for t in terms if t["source"] in text]
        p.save(reviews / f"{offset//24+1:04}.json", {"glossary": relevant, "paragraphs": group})
    p.log(f"Quality candidates {len(flags)}; semantic review {len(selected)} paragraphs in {(len(selected)+23)//24} batches")


def review(workers):
    paths = sorted((p.ROOT / "Reviews").glob("*.json"))
    def one(path):
        payload = p.read(path)
        result = p.invoke("audit", path.stem, INSTRUCTIONS, payload, SCHEMA, 10000)
        allowed = {i["id"] for i in payload["paragraphs"]}
        for c in result["corrections"]:
            if c["id"] not in allowed or not c["text"].strip() or p.CJK.search(c["text"]):
                raise ValueError(f"Invalid correction in {path.stem}")
        return result["corrections"]
    corrections = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = [pool.submit(one, path) for path in paths]
        for f in as_completed(jobs):
            corrections.extend(f.result())
    corrections.sort(key=lambda x: x["id"])
    if len({x["id"] for x in corrections}) != len(corrections):
        raise ValueError("Duplicate correction")
    p.save(p.ROOT / "Reviewproposals.json", corrections)
    p.save(p.ROOT / "Reviewstate.json", {"sourcehash": p.read(p.ROOT / "Source.json")["sourcehash"],
                                      "translationhash": translation_hash()})
    p.log(f"Review proposals: {len(corrections)}; inspect them before running apply")


def translation_hash():
    return p.digest(p.json.dumps({i["id"]: i["translation"] for i in load_items()}, sort_keys=True, ensure_ascii=False))


def apply():
    state = p.read(p.ROOT / "Reviewstate.json")
    if state["sourcehash"] != p.read(p.ROOT / "Source.json")["sourcehash"] or state["translationhash"] != translation_hash():
        raise ValueError("Review proposals are stale; review the current translations again")
    corrections = p.read(p.ROOT / "Reviewproposals.json")
    source_by_id = {i["id"]: i["source"] for i in p.read(p.ROOT / "Source.json")["items"]}
    allowed = set(source_by_id)
    selection = p.ROOT / "Reviewselection.json"
    if selection.exists():
        rejected = {x["id"] for x in p.read(selection)}
        if not rejected <= {x["id"] for x in corrections}:
            raise ValueError("Rejected id is not a review proposal")
        corrections = [x for x in corrections if x["id"] not in rejected]
    overrides = p.ROOT / "Reviewoverrides.json"
    if overrides.exists():
        merged = {x["id"]: x for x in corrections}
        for correction in p.read(overrides):
            merged[correction["id"]] = correction
        corrections = sorted(merged.values(), key=lambda x: x["id"])
    for correction in corrections:
        if correction["id"] not in allowed or not correction["text"].strip() or p.CJK.search(correction["text"]):
            raise ValueError("Invalid review correction")
        if not p.valid_chapter(source_by_id[correction["id"]], correction["text"]):
            raise ValueError("Review correction changes a chapter number; reject or override that suggestion")
    p.save(p.ROOT / "Corrections.json", {**state, "items": corrections})
    p.log(f"Semantic corrections: {len(corrections)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["check", "review", "apply"])
    p.add_options(parser)
    parser.add_argument("--all", action="store_true", help="Prepare semantic review of every paragraph")
    args = parser.parse_args()
    p.configure(args)
    if args.stage == "check": check(args.all)
    elif args.stage == "review": review(args.workers)
    else: apply()
