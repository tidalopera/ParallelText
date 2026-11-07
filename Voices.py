"""Load a bundled Edge voice catalogue; optionally refresh its private copy."""
import argparse
import asyncio
import json
from pathlib import Path
import re

import edge_tts

BASE = Path(__file__).resolve().parent


def normalize_voices(rows):
    result = {}
    for row in rows:
        short = row.get("ShortName", "")
        locale = row.get("Locale", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{3,120}", short) or not locale:
            raise ValueError("Invalid Edge voice catalogue entry")
        name = short[len(locale)+1:].removesuffix("Neural")
        result[short] = {"id": short, "name": name, "locale": locale,
                         "gender": row.get("Gender", "Unknown"), "provider": "edge"}
    if not result:
        raise ValueError("Empty Edge voice catalogue")
    return sorted(result.values(), key=lambda voice: (voice["locale"], voice["name"]))


def load_catalogue(cache=None, bundled=None):
    for path in (cache or BASE/"Work/Voices.json", bundled or BASE/"Voices.json"):
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            if not isinstance(rows, list) or not rows:
                continue
            if any(not isinstance(v, dict) or not isinstance(v.get("name"), str)
                   or not re.fullmatch(r"[A-Za-z0-9-]{3,120}", v.get("id", ""))
                   or not isinstance(v.get("locale"), str) for v in rows):
                continue
            return rows
        except (OSError, ValueError, TypeError):
            continue
    return []


async def refresh_catalogue(destination):
    voices = normalize_voices(await asyncio.wait_for(edge_tts.list_voices(), timeout=25))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(voices, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    temporary.replace(destination)
    return voices


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    voices = asyncio.run(refresh_catalogue(BASE/"Work/Voices.json")) if args.refresh else load_catalogue()
    print(f"Edge voices: {len(voices)}")
