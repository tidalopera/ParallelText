# ParallelText

Glossary-guided Chinese-to-English translation, with a local bilingual reader and word-level audio highlighting.

ParallelText keeps each nonempty source line paired with its translation. It extracts terminology before translating, saves resumable batches, and separates review proposals from approved changes. The reader displays both languages, supports search and continuous listening, and skips punctuation-only paragraphs without stopping playback.

[中文说明](ReadmeChinese.md)

## Try the reader first

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13 or newer, then run:

```sh
git clone https://github.com/tidalopera/ParallelText.git
cd ParallelText
uv sync --locked
uv run python Reader.py
```

Open <http://127.0.0.1:8766> yourself. The original, included demonstration needs no model credentials. Reading and search are local; speech generation uses the online `edge-tts` service and sends the selected English paragraph to that service.

On Windows, `StartReader.vbs` starts the demo service with a hidden console. It does not open a browser. To read your own translated workspace:

```sh
uv run python Reader.py --book Work/Book --port 8766
```

The reader checks that `Aligned.json` and `English.txt` match before loading. Stop the current service with its “关闭本地服务” button before using a different book on the same port, or choose another port.

If `Work/Book/Aligned.json` exists, the default reader and Windows launcher load that private workspace on port 8765; otherwise they load the included demo on port 8766. The CLI can override the port with `--port`.

While preparing the current paragraph, the player starts generating the next six speakable paragraphs. Two background workers handle prefetching; separate foreground workers and promotion of queued jobs keep speculative work from blocking a newly selected paragraph. Symbol-only lines do not consume the lookahead. Audio remains cached for the service lifetime: the first uncached paragraph, a distant jump, or a new voice can still require a network wait.

## Translate a text

The translator uses the OpenAI Responses API. A custom `OPENAI_BASE_URL` must implement streaming Responses and strict JSON-schema output; a Chat Completions-only endpoint will not work. Model names are configurable, and access and charges depend on your provider.

1. Copy `Examples/Settings.json` to local `Config.json`. Replace both `YOUR_..._MODEL` placeholders with models available to you. `reasoningEffort` defaults to `null`: set it only for models that support it.
2. Set your API key in the environment. For example, in PowerShell:

```powershell
$env:OPENAI_API_KEY = "your-api-key"
# Optional:
# $env:OPENAI_BASE_URL = "https://your-provider.example/v1"
```

3. Run the pipeline. These commands use the included original sample; replace `--source` with a TXT file you have permission to translate.

```sh
uv run python Pipeline.py prepare --source Examples/Source.txt --work Work/Book
uv run python Pipeline.py scan --work Work/Book
uv run python Pipeline.py unify --work Work/Book
```

Inspect `Work/Book/Glossary.tsv`. Edit canonical targets in `Work/Book/Glossary.json` before translating; JSON is the input used by the translator.

```sh
uv run python Pipeline.py translate --work Work/Book --workers 4
uv run python Quality.py check --work Work/Book --all
uv run python Quality.py review --work Work/Book --workers 4
```

Inspect `Reviewproposals.json`. Reviewing does **not** apply suggestions. Optionally create `Reviewselection.json` to reject suggestions:

```json
[{"id": 42, "reason": "The initial translation is accurate in context."}]
```

Only use IDs actually proposed for your text. Optional `Reviewoverrides.json` is a list of manually approved replacements, each with `id`, complete English `text`, and `reason`.

```sh
uv run python Quality.py apply --work Work/Book
uv run python Pipeline.py assemble --work Work/Book
uv run python Reader.py --book Work/Book
```

To export an initial translation without review, run `assemble` after `translate` and before creating any applied corrections. Omit `--all` from `check` for a cheaper review set containing all rule-flagged paragraphs, a deterministic random sample, and opening/closing paragraphs. `review` processes exactly the files prepared by `check`.

You can override models with `--model` and `--analysis-model`, or use `OPENAI_MODEL` for a shared model. `--config` selects a different settings file. Use `--encoding gb18030` or another explicit codec when needed; `auto` tries strict UTF-8, then GB18030. Decoding success alone does not prove the selected encoding is correct, so inspect the prepared source before sending a large book.

## What is preserved

- Each nonempty physical source line gets an ID. Leading/trailing whitespace is trimmed; original blank-line layout is not preserved.
- A request contains several numbered paragraphs, a small window of neighboring source paragraphs, and the matching glossary entries. There is no shared conversation across batches.
- Output IDs must match exactly, in order. Empty translations and residual Chinese are rejected.
- Chapter numbers, notes, fragments and punctuation-only lines are retained. No automatic advertising deletion is performed.
- `Bilingual.txt` alternates source, translation, and a blank separator. `English.txt` contains English paragraphs; `Aligned.json` contains their pairing.
- The source file is read-only. Reusing a workspace with a different source or batch layout is rejected. Applied corrections are rejected if their translation snapshot becomes stale.

Caches fingerprint the model, endpoint, reasoning setting, prompt, schema and payload. Retrying a matching completed batch reuses saved output. Structural validation and model review cannot guarantee semantic accuracy; review valuable or sensitive material yourself.

## Files and privacy

| Location | Purpose |
| --- | --- |
| `Pipeline.py` | Prepare, extract/unify terminology, translate, assemble |
| `Quality.py` | Flag risks, prepare review, collect proposals, apply approved changes |
| `Reader.py`, `Reader.js`, `Reader.html`, `Reader.css` | Loopback reader, speech jobs and word highlighting |
| `Examples/` | Original demonstration source, paired text and sample settings |
| `Work/` | Private source snapshots, prompts/responses, translations and review records; ignored by Git |
| `Config.json` | Your local settings; ignored by Git |

The reader binds to `127.0.0.1` and validates Host/Origin. It is designed for local personal use, not public hosting. Audio is cached in a temporary directory for the service lifetime. Translation sends source content to your chosen model endpoint; TTS sends selected English text to its online service. This is not a fully offline application.

If you already have a trusted local authentication module exporting `access_token()`, set `PARALLELTEXT_AUTH_MODULE` to its file path. That file is imported as Python code: only select a module you trust. No authentication module, account tokens or user credentials are bundled.

Alternatively set `authModule` in your ignored local `Config.json`. It is resolved by the same interpreter as the project; no separate environment is launched.

Keep private input inside `Work/`; arbitrary files elsewhere are not automatically private. Check `git status` before publishing your own copy. The software license does not grant rights to third-party books or their translations.

## Checks

```sh
uv run python -m unittest discover -s Tests -p "Test*.py" -v
uv run python ReaderCheck.py
node ReaderCheck.cjs
uv run python Reader.py --check
```

Node.js is needed only for the frontend regression checks. They exercise the real playback handlers with a small simulated DOM, including consecutive ellipses, seek boundaries, end-of-book behavior and actual service-error handling. They do not replace browser or listening tests.

## License

MIT. The demonstration source and translations were created for this project and are included under the same license. No third-party book content, user glossary, model-response archive or credential is included.
