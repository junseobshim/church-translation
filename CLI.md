# CLI Reference

Direct command-line usage for `main.py`. The recommended way to run the system is via the control panel (see README); these commands are for developer use.

## Running

```bash
# Korean (mixed with English) → English  (default)
python main.py

# English → Korean
python main.py --source en --target ko

# Korean → English AND Spanish (parallel translation streams on separate URLs)
python main.py --source ko --target en,es

# Spanish (mixed with English) → English
python main.py --source es --target en

# Multilingual (ko + en + es speech) → all three translation streams
python main.py --source multi
```

You'll be prompted to select an audio input device, then transcription and translation begin immediately. A web caption server starts on port 8080 by default.

`--source` picks Soniox's strict language hints: `ko` → `[ko, en]`, `en` → `[en]`, `es` → `[es, en]`, `multi` → `[ko, en, es]`. `--target` accepts a comma-separated subset of `{ko, en, es}`; `--source multi` is fixed at `--target ko,en,es`. `--target` is required for `--source en` and `--source es`; defaults exist only for `ko` (→ `en`) and `multi` (→ `ko,en,es`). Bilingual sources (`ko`, `es`) may include their own base language as a same-language passthrough target (e.g. `--source ko --target ko,en`) — matching segments pass through untranslated. Only `--source en` cannot target `en`.

### Sermon Outline (optional)

If you have the sermon outline ahead of time, pass it with `--outline` to give Claude topical and structural context. This also activates Anthropic prompt caching, making every subsequent translation call cheaper and slightly faster.

```bash
python main.py --outline path/to/sermon.txt
```

- On the command line, `--outline` takes a **UTF-8 plain-text** file. (The control panel additionally accepts a Word `.docx` upload, which it converts to text in the browser before sending — so `.docx` works from the panel, not from the `--outline` flag.) Any `.txt` with bullet points, verse references, or prose works. For a multilingual sermon, use a single multilingual outline; it is attached verbatim to every target worker's system prompt.
- Caching activates only when the combined system prompt + outline exceeds 1024 tokens (roughly 700–800 words). Below that, the script warns on stderr and runs without caching.
- With multiple `--target` languages, each target worker caches its own system-prompt + outline independently and has its own keep-alive ping. Expect one `Cache warmed` message per cached worker at startup.
- The cache has a 5-minute lifetime between calls. A keep-alive ping fires every 4m30s of silence so the cache survives long pauses.
- The outline is used as **context only** — Claude is instructed to translate what is actually said, even when the speaker rhetorically diverges from the outline.



## CLI Options


| Flag                        | Default                                                                       | Description                                                                                                                                                                                                 |
| --------------------------- | ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--source {ko,en,es,multi}` | `ko`                                                                          | Source language. `ko` = Korean + English, `en` = English only, `es` = Spanish + English, `multi` = Korean + English + Spanish. Sets Soniox's strict language hints.                                         |
| `--target CSV`              | `en` when `--source ko`, `ko,en,es` when `--source multi`; required otherwise | Comma-separated translation targets. Must be a non-empty subset of `{ko,en,es}`. Bilingual sources (`ko`, `es`) may include their own base language as a passthrough target; `--source en` cannot target `en`. For `--source multi`, must be exactly `ko,en,es`. Each target runs as its own parallel Claude worker. |
| `--device N`                | (interactive)                                                                 | Audio input device index (skip selection prompt)                                                                                                                                                            |
| `--port PORT`               | `8080`                                                                        | Web caption server port (`0` to disable)                                                                                                                                                                    |
| `--tunnel NAME`             | `church-live`                                                                 | Cloudflare tunnel name to start                                                                                                                                                                             |
| `--no-tunnel`               | —                                                                             | Skip starting the Cloudflare tunnel                                                                                                                                                                         |
| `--outline PATH`            | —                                                                             | Path to a UTF-8 `.txt` sermon outline. Enables per-target prompt caching when the combined system prompt exceeds 1024 tokens.                                                                               |
| `--transcriber {soniox}`    | `soniox`                                                                      | Transcription backend. Loads `transcribe_<name>.py` at startup. More options will be added as alternative backends land (e.g. `mlx-whisper`, `azure`).                                                      |
| `--translator {claude}`     | `claude`                                                                      | Translation backend. Loads `translate_<name>.py` at startup. More options will be added as alternative backends land (e.g. `qwen-mlx`, `gemini`).                                                           |

**Environment variable:** Set `CLAUDE_MODEL` in `.env` to override the translation model (e.g. `CLAUDE_MODEL=claude-opus-4-8`). Applies at startup; takes precedence over the backend's `DEFAULT_MODEL`.
