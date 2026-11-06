"""ParallelText: resumable, glossary-guided Chinese-to-English translation."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
ROOT = BASE / "Work" / "Book"
SOURCE = None
SETTINGS = {}
from openai import OpenAI

PRINTLOCK = threading.Lock()
CJK = re.compile(r"[\u3400-\u9fff]")
CHAPTER = re.compile(r"^(?:第[零〇一二三四五六七八九十百千万两\d]+(?:[章卷节]|\s)|Chapter\s+\d+\b)", re.I)
MODELS = {stage: os.getenv("OPENAI_MODEL", "") for stage in ("scan", "unify", "translate", "audit")}


def add_options(parser):
    parser.add_argument("--work", type=Path, default=BASE / "Work" / "Book", help="Private book workspace")
    parser.add_argument("--config", type=Path, help="Local JSON settings; defaults to Config.json if present")
    parser.add_argument("--source", type=Path, help="Input TXT path, required for prepare")
    parser.add_argument("--encoding", default="auto", help="auto (UTF-8, then GB18030), or an explicit encoding")
    parser.add_argument("--model", help="Translation model (also used for analysis unless overridden)")
    parser.add_argument("--analysis-model", help="Glossary and review model")
    parser.add_argument("--workers", type=int, default=4)


def configure(args):
    global ROOT, SOURCE, SETTINGS
    ROOT = args.work.expanduser().resolve()
    config = args.config or (BASE / "Config.json" if (BASE / "Config.json").exists() else None)
    SETTINGS = read(config) if config else {}
    SETTINGS["encoding"] = args.encoding
    translation = args.model or SETTINGS.get("translationModel") or os.getenv("OPENAI_MODEL", "")
    analysis = args.analysis_model or SETTINGS.get("analysisModel") or translation
    MODELS.update(translate=translation, scan=analysis, unify=analysis, audit=analysis)
    if args.workers < 1 or args.workers > 32:
        raise ValueError("workers must be between 1 and 32")
    SOURCE = args.source.expanduser().resolve() if args.source else None
    if SOURCE is None and (ROOT / "Source.json").exists():
        SOURCE = Path(read(ROOT / "Source.json")["source"])


def access_token():
    """Credentials remain in environment or an explicitly selected local provider."""
    provider = os.getenv("PARALLELTEXT_AUTH_MODULE")
    if provider:
        spec = importlib.util.spec_from_file_location("paralleltext_auth", Path(provider).expanduser().resolve())
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load PARALLELTEXT_AUTH_MODULE")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.access_token()
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set OPENAI_API_KEY or PARALLELTEXT_AUTH_MODULE before model calls")
    return key


def base_url():
    return os.getenv("OPENAI_BASE_URL") or SETTINGS.get("baseUrl", "https://api.openai.com/v1")


def chapter_number(source):
    english = re.match(r"^Chapter\s+(\d+)\b", source, re.I)
    if english:
        return int(english.group(1))
    match = re.match(r"^第([零〇一二三四五六七八九十百千万两\d]+)(?:[章卷节]|(?=\s))", source)
    if not match:
        return None
    number = match.group(1)
    if number.isdigit():
        return int(number)
    digits = dict(zip("零〇一二三四五六七八九两", [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
    units = {"十": 10, "百": 100, "千": 1000}
    total = section = current = 0
    for char in number:
        if char in digits:
            current = digits[char]
        elif char == "万":
            total += (section + current or 1)*10000
            section = current = 0
        else:
            section += (current or 1)*units[char]
            current = 0
    return total+section+current


def valid_chapter(source, target):
    expected = chapter_number(source)
    if expected is None:
        return True
    got = re.match(r"^Chapter\s+(\d+)\b", target, re.I)
    return bool(got and int(got.group(1)) == expected)


def now():
    return datetime.now(timezone.utc).isoformat()


def log(message):
    with PRINTLOCK:
        print(now()[11:19], message, flush=True)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TERM_SCHEMA = object_schema({k: {"type": "string"} for k in ("source", "target", "kind", "note")})
TERMS_SCHEMA = object_schema({"terms": {"type": "array", "items": TERM_SCHEMA}})
TRANSLATION_SCHEMA = object_schema({"items": {"type": "array", "items": object_schema({"id": {"type": "integer"}, "text": {"type": "string"}})}})


def invoke(stage, label, instructions, payload, schema, limit=12000):
    model = MODELS[stage]
    if not model or model.startswith("YOUR_"):
        raise ValueError("Choose models with --model/--analysis-model, OPENAI_MODEL, or Config.json")
    instructions += "\n" + SETTINGS.get("glossaryRules" if stage in ("scan", "unify") else "translationRules", "")
    body = json.dumps(payload, ensure_ascii=False)
    effort = SETTINGS.get("reasoningEffort")
    fingerprint = digest(instructions + body + json.dumps(schema, sort_keys=True) + model + base_url() + str(effort))
    cached = ROOT / "Responses" / f"{stage}{label}.json"
    if cached.exists():
        existing = read(cached)
        if existing.get("fingerprint") == fingerprint:
            return existing["result"]
    for attempt in range(5):
        started = time.monotonic()
        try:
            client = OpenAI(api_key=access_token(), base_url=base_url(), max_retries=0, timeout=600)
            final = None
            deltas = []
            with client.responses.create(
                model=model, instructions=instructions,
                input=[{"role": "user", "content": body}], store=False, stream=True,
                max_output_tokens=limit,
                text={"format": {"type": "json_schema", "name": stage, "strict": True, "schema": schema}},
                **({"reasoning": {"effort": effort}} if effort else {}),
            ) as stream:
                for event in stream:
                    if event.type == "response.output_text.delta":
                        deltas.append(event.delta)
                    elif event.type == "response.completed":
                        final = event.response
                    elif event.type in {"response.failed", "response.incomplete", "error"}:
                        detail = getattr(getattr(event, "response", None), "incomplete_details", None)
                        raise RuntimeError(f"{event.type}: {detail}")
            if final is None:
                raise RuntimeError("No completed response")
            output_text = final.output_text or "".join(deltas)
            if not output_text:
                refusals = [c.refusal for o in final.output if hasattr(o, "content") for c in o.content if getattr(c, "type", "") == "refusal"]
                if refusals:
                    raise ValueError("Model refusal; requires review")
                raise RuntimeError("Empty output")
            result = json.loads(output_text)
            save(cached, {"fingerprint": fingerprint, "model": model, "reasoning": effort, "responseid": final.id,
                          "timestamp": now(), "seconds": round(time.monotonic()-started, 2),
                          "usage": final.usage.model_dump() if final.usage else {}, "result": result})
            log(f"{stage} {label} OK {time.monotonic()-started:.0f}s")
            return result
        except Exception as error:
            status = getattr(error, "status_code", None)
            # Never log credentials, request headers or raw HTTP response bodies.
            message = str(error)[:240] if isinstance(error, (ValueError, RuntimeError, json.JSONDecodeError)) else f"{type(error).__name__} HTTP {status}"
            log(f"{stage} {label} attempt {attempt+1}: {message}")
            if status in (401, 403) or "refusal" in message.lower():
                raise RuntimeError(message) from None
            if attempt == 4:
                raise RuntimeError(message) from None
            time.sleep(min(8 * 2**attempt, 90))


def prepare():
    if SOURCE is None:
        raise ValueError("prepare requires --source path/to/book.txt")
    if ROOT == BASE or ROOT == SOURCE.parent:
        raise ValueError("Use a separate --work directory for generated book data")
    raw = SOURCE.read_bytes()
    encoding = SETTINGS.get("encoding", "auto")
    if encoding == "auto":
        try:
            text = raw.decode("utf-8-sig", errors="strict")
            encoding = "utf-8-sig"
        except UnicodeDecodeError:
            text = raw.decode("gb18030", errors="strict")
            encoding = "gb18030"
    else:
        text = raw.decode(encoding, errors="strict").lstrip("\ufeff")
    if "\ufffd" in text:
        raise ValueError("Input contains replacement characters; check the source encoding")
    sizes = {"Scans": int(SETTINGS.get("scanChars", 68000)), "Batches": int(SETTINGS.get("batchChars", 6800))}
    if min(sizes.values()) < 1:
        raise ValueError("scanChars and batchChars must be positive")
    if (ROOT / "Source.json").exists():
        previous = read(ROOT / "Source.json")
        if previous["sourcehash"] != hashlib.sha256(raw).hexdigest() or previous.get("groupSizes") != sizes:
            raise ValueError("Workspace already contains a different source or batch layout; choose a new --work directory")
    lines = text.splitlines()
    items = []
    chapter = "Front matter"
    for row, line in enumerate(lines):
        if not line.strip():
            continue
        if CHAPTER.match(line.strip()):
            chapter = line.strip()
        items.append({"id": len(items), "row": row, "source": line.strip(), "chapter": chapter})
    if not items:
        raise ValueError("Input contains no nonempty paragraphs")
    data = {"source": str(SOURCE), "sourcehash": hashlib.sha256(raw).hexdigest(), "encoding": encoding, "groupSizes": sizes,
            "target": "English", "physicalLines": len(lines), "items": items}
    save(ROOT / "Source.json", data)
    for name, size in sizes.items():
        groups, current, chars = [], [], 0
        for item in items:
            if current and (chars + len(item["source"]) > size or (name == "Batches" and CHAPTER.match(item["source"]))):
                groups.append(current)
                current, chars = [], 0
            current.append(item)
            chars += len(item["source"])
        if current:
            groups.append(current)
        for i, group in enumerate(groups):
            save(ROOT / name / f"{i+1:04}.json", group)
        log(f"Prepared {name}: {len(groups)} groups")
    log(f"Prepared {len(items)} nonempty source lines; encoding={encoding}")


SCAN_INSTRUCTIONS = """Prepare Chinese prose for a faithful English translation. Extract a concise glossary FROM THE PROVIDED TEXT. Include important names, meaningful aliases, locations, organizations, recurring specialized vocabulary, and titles whose distinctions matter. Exclude complete chapter headings: their numbering and format are handled separately. Select at most 65 high-value entries, not ordinary words. source MUST be an exact substring in the source. Give one natural canonical English target, category in kind, and a short evidence-based note on identity, pronouns or ambiguity. Use Pinyin for personal names unless supplied rules specify otherwise; translate meaningful nicknames naturally. Do not infer identity or gender without textual evidence. Do not conflate different titles. Output only the required JSON."""


def scan(workers):
    paths = sorted((ROOT / "Scans").glob("*.json"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(invoke, "scan", p.stem, SCAN_INSTRUCTIONS, {"text": "\n".join(x["source"] for x in read(p))}, TERMS_SCHEMA, 10000): p for p in paths}
        for future in as_completed(jobs):
            future.result()


def unify():
    corpus = "\n".join(x["source"] for x in read(ROOT / "Source.json")["items"])
    entries = {}
    for path in sorted((ROOT / "Responses").glob("scan*.json")):
        for entry in read(path)["result"]["terms"]:
            source = entry["source"].strip()
            if source and source in corpus and not CHAPTER.match(source):
                entries.setdefault(source, []).append(entry)
    candidates = []
    for source, variants in entries.items():
        candidates.append({"source": source, "frequency": corpus.count(source), "proposals": variants})
    candidates.sort(key=lambda x: (-x["frequency"], x["source"]))
    save(ROOT / "Candidates.json", candidates)
    instructions = """Build a canonical glossary for faithful Chinese-to-English translation. Input contains exact source terms, full-text frequencies, and evidence-based extraction proposals. Resolve conflicting spellings; choose one target per source. Preserve useful aliases as separate entries. Omit ordinary words and extraction noise, but retain important named entities even if rare. Use Pinyin for personal names and natural readable English for meaningful nicknames unless supplied rules specify otherwise. Preserve distinctions between titles, identities and specialist terms. Do not invent gender or identity. Source must be copied exactly from candidates. Include all useful terminology, not a sample. Output only required JSON."""
    result = invoke("unify", "All", instructions, {"candidates": candidates}, TERMS_SCHEMA, 22000)
    seen, good = set(), []
    for entry in result["terms"]:
        if entry["source"] not in entries or entry["source"] in seen:
            raise ValueError("Invalid or duplicate canonical term")
        seen.add(entry["source"])
        good.append(entry)
    good.sort(key=lambda x: (-len(x["source"]), x["source"]))
    save(ROOT / "Glossary.json", {"sourcehash": read(ROOT / "Source.json")["sourcehash"], "terms": good})
    (ROOT / "Glossary.tsv").write_text("Source\tEnglish\tCategory\tNotes\n" + "\n".join("\t".join(str(e[k]).replace("\t", " ").replace("\n", " ") for k in ("source", "target", "kind", "note")) for e in good), encoding="utf-8-sig")
    log(f"Canonical glossary: {len(good)} terms from {len(candidates)} candidates")


TRANSLATE_INSTRUCTIONS = """Translate the numbered Chinese paragraphs into fluent, faithful English. Preserve all content, meaning, tone, dialogue, fragments, messages, headings and author notes. Do not summarize, explain, sanitize, embellish, omit, merge or split paragraphs. Keep each id exactly once in order, with English text for that id only. Context is for reference, not translation. Respect the canonical glossary; longer exact source matches take priority over shorter ones. Preserve distinctions between names, aliases, titles and identities. Preserve numerical values and brackets. Do not invent context or introduce Chinese text into English translations. For chapter headings use Chapter + Arabic number + title, preserving the source number including inconsistencies. Render source errors by the best-supported meaning without inventing events or adding editorial notes. Output only required JSON, with exactly the requested ids."""


def translate_one(path, allitems, glossary):
    batch = read(path)
    output = ROOT / "Translations" / path.name
    inputhash = digest(json.dumps(batch, ensure_ascii=False) + json.dumps(glossary, ensure_ascii=False) + TRANSLATE_INSTRUCTIONS
                       + json.dumps(SETTINGS, sort_keys=True) + MODELS["translate"] + base_url())
    if output.exists() and read(output).get("inputhash") == inputhash:
        return
    text = "\n".join(i["source"] for i in batch)
    context = allitems[max(0, batch[0]["id"]-3):batch[0]["id"]]
    after = allitems[batch[-1]["id"]+1:batch[-1]["id"]+3]
    relevant = [e for e in glossary if e["source"] in text or any(e["source"] in c["source"] for c in context)]
    payload = {"chapter": batch[0]["chapter"], "glossary": relevant,
               "preceding_context": [i["source"] for i in context], "following_context": [i["source"] for i in after],
               "paragraphs": [{"id": i["id"], "text": i["source"]} for i in batch]}
    expected = [i["id"] for i in batch]
    source_by_id = {i["id"]: i["source"] for i in batch}
    errors = []
    for attempt in range(3):
        label = path.stem + (f"Retry{attempt}" if attempt else "")
        result = invoke("translate", label, TRANSLATE_INSTRUCTIONS, payload, TRANSLATION_SCHEMA, 16000)
        got = result["items"]
        errors = []
        if [i["id"] for i in got] != expected:
            errors.append("Return exactly the requested paragraph ids, in their original order, with no omissions or extras.")
        for item in got:
            if not item["text"].strip():
                errors.append(f"Empty paragraph {item['id']}")
            if CJK.search(item["text"]):
                errors.append(f"Chinese remained untranslated in paragraph {item['id']}; all prose and names must be English.")
            if item["id"] in source_by_id and not valid_chapter(source_by_id[item["id"]], item["text"]):
                errors.append(f"Paragraph {item['id']} must start with Chapter and the original chapter number written in Arabic digits.")
            if re.search(r"\b(I cannot translate|I can't translate)\b", item["text"], re.I):
                raise ValueError("Refusal-like content requires review")
        if not errors:
            save(output, {"inputhash": inputhash, "model": MODELS["translate"], "items": got})
            return
        payload["correction"] = errors[:15]
        log(f"Structural retry {path.stem}: {errors[:3]}")
    raise ValueError(f"Validation failed {path.stem}: {errors[:4]}")


def translate(workers, first=0):
    allitems = read(ROOT / "Source.json")["items"]
    glossary = read(ROOT / "Glossary.json")["terms"]
    paths = sorted((ROOT / "Batches").glob("*.json"))
    if first:
        paths = paths[:first]
    complete, errors = 0, []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(translate_one, p, allitems, glossary): p for p in paths}
        for future in as_completed(jobs):
            try:
                future.result()
                complete += 1
                if complete % 5 == 0 or first:
                    log(f"Progress {complete}/{len(paths)}")
            except Exception as e:
                errors.append({"batch": jobs[future].name, "error": str(e)[:300]})
                log(f"Batch FAILED {jobs[future].name}: {str(e)[:160]}")
    save(ROOT / "Runstatus.json", {"timestamp": now(), "complete": complete, "total": len(paths), "errors": errors})
    if errors:
        raise ValueError(f"{len(errors)} batches failed; checkpoints preserved")


def assemble():
    source = read(ROOT / "Source.json")
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != source["sourcehash"]:
        raise ValueError("Source changed")
    translated = {}
    for path in sorted((ROOT / "Translations").glob("*.json")):
        for item in read(path)["items"]:
            if item["id"] in translated:
                raise ValueError("Duplicate translated id")
            translated[item["id"]] = item["text"]
    expected = set(range(len(source["items"])))
    if set(translated) != expected:
        raise ValueError(f"Missing ids {sorted(expected-set(translated))[:20]}")
    corrections_path = ROOT / "Corrections.json"
    if corrections_path.exists():
        bundle = read(corrections_path)
        if bundle["sourcehash"] != source["sourcehash"] or bundle["translationhash"] != digest(json.dumps(translated, sort_keys=True, ensure_ascii=False)):
            raise ValueError("Review corrections are stale; review the current translations before applying them")
        for correction in bundle["items"]:
            if correction["id"] not in expected:
                raise ValueError("Correction id outside source")
            translated[correction["id"]] = correction["text"]
    exclusions_path = ROOT / "Exclusions.json"
    excluded = {item["id"] for item in read(exclusions_path)["items"]} if exclusions_path.exists() else set()
    if not excluded <= expected:
        raise ValueError("Exclusion id outside source")
    paragraphs = []
    english = []
    aligned = []
    for item in source["items"]:
        if item["id"] in excluded:
            continue
        dst = " ".join(translated[item["id"]].splitlines()).strip()
        if not dst or CJK.search(dst):
            raise ValueError(f"Invalid translated paragraph {item['id']}")
        if not valid_chapter(item["source"], dst):
            raise ValueError(f"Invalid chapter number in paragraph {item['id']}")
        paragraphs.extend([item["source"], dst, ""])
        english.extend([dst, ""])
        aligned.append({**item, "translation": dst})
    (ROOT / "Bilingual.txt").write_text("\n".join(paragraphs) + "\n", encoding="utf-8-sig")
    (ROOT / "English.txt").write_text("\n".join(english) + "\n", encoding="utf-8-sig")
    save(ROOT / "Aligned.json", aligned)
    usage = Counter()
    for path in (ROOT / "Responses").glob("*.json"):
        for key, value in read(path).get("usage", {}).items():
            if isinstance(value, int):
                usage[key] += value
    save(ROOT / "Validation.json", {"timestamp": now(), "sourcehash": source["sourcehash"], "paragraphs": len(aligned),
                                    "sourceParagraphs": len(source["items"]), "excludedParagraphs": len(excluded),
                                    "translated": len(aligned), "cachedTranslations": len(translated), "missing": 0, "duplicates": 0, "remainingChinese": 0,
                                    "glossaryTerms": len(read(ROOT / "Glossary.json")["terms"]), "usage": dict(usage)})
    log(f"Assembled {len(aligned)} aligned paragraphs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["prepare", "scan", "unify", "translate", "assemble"])
    add_options(parser)
    parser.add_argument("--first", type=int, default=0)
    args = parser.parse_args()
    configure(args)
    if args.stage == "prepare": prepare()
    elif args.stage == "scan": scan(args.workers)
    elif args.stage == "unify": unify()
    elif args.stage == "translate": translate(args.workers, args.first)
    else: assemble()


if __name__ == "__main__":
    main()
