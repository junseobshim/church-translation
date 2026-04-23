# Live Church Sermon Translation

Real-time sermon translation using [Soniox](https://soniox.com/) real-time STT and [Claude](https://anthropic.com/) for translation, with a built-in web display for ProPresenter or any browser. Supports Korean→English and English→Korean.

## Prerequisites

- macOS with [Homebrew](https://brew.sh/)
- A [Soniox API key](https://soniox.com/) (real-time speech-to-text)
- An [Anthropic API key](https://console.anthropic.com/) (Claude translation)
- An audio input device (e.g. USB interface from church soundboard)

## Setup

```bash
# Install dependencies (skip any you already have)
brew install python git portaudio

# Clone the repo
git clone https://github.com/junseobshim/church-translation.git
cd church-translation

# Create a virtual environment and install Python packages
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure API keys
cp .env.example .env   # then edit .env and fill in SONIOX_API_KEY and ANTHROPIC_API_KEY
```

## Running

```bash
# Korean → English (default)
python soniox_claude.py

# English → Korean
python soniox_claude.py --lang en
```

You'll be prompted to select an audio input device, then transcription and translation begin immediately. A web caption server starts on port 8080 by default.

### Sermon Outline (optional)

If you have the sermon outline ahead of time, pass it with `--outline` to give Claude topical and structural context. This also activates Anthropic prompt caching, making every subsequent translation call cheaper and slightly faster.

```bash
python soniox_claude.py --outline path/to/sermon.txt
```

- The file must be UTF-8 plain text. Any `.txt` with bullet points, verse references, or prose works.
- Caching activates only when the combined system prompt + outline exceeds 1024 tokens (roughly 700–800 words). Below that, the script warns on stderr and runs without caching.
- The cache has a 5-minute lifetime between calls. A keep-alive ping fires every 4m30s of silence so the cache survives long pauses.
- The outline is used as **context only** — Claude is instructed to translate what is actually said, even when the speaker rhetorically diverges from the outline.

### Application
Use the below script (audio device 4, default CLI flags otherwise) with Automator (Application, Run Shell Script) for one click execution (.app file, pinnable to dock)

`osascript -e "tell application \"Terminal\" to do script \"cd $HOME/Documents/church-translation && source venv/bin/activate && python soniox_claude.py --device 4\""`

## Web Display

Open in any browser or ProPresenter Web Fill:

| URL | What it shows |
|-----|---------------|
| `http://localhost:8080/` | Transcriptions in the sermon's source language, line-by-line. The default `lang` filter matches whatever `--lang` the script was launched with, so no query params are needed. |
| `http://localhost:8080/?mode=translation&lang=en` | English translations only |
| `http://localhost:8080/?display=paragraph` | Paragraph style (for ProPresenter) |
| `http://localhost:8080/?mode=translation&lang=en&display=paragraph` | English translations, paragraph style |
| `http://localhost:8080/?mode=translation&lang=en&display=paragraph&fontSize=98&fontWeight=500&lineSpacing=1.3` | English translations default for RCC Sanctuary TV display

### Query Parameters

| Param | Default | Description |
|-------|---------|-------------|
| `mode` | `transcription` | `transcription` or `translation` |
| `lang` | source language of current session | ISO 639-1 language filter. Defaults to whatever `--lang` the script was launched with. Explicit value always wins. |
| `display` | `line` | `line` (block divs) or `paragraph` (inline spans) |
| `fontSize` | `48` | Font size in px |
| `fontFamily` | `system-ui, sans-serif` | CSS font stack |
| `googleFont` | — | Google Fonts name (auto-loaded) |
| `fontWeight` | `normal` | CSS font weight |
| `color` | `white` | Text color |
| `lineSpacing` | `1.4` | CSS line-height |
| `textAlign` | `left` | CSS text-align |
| `textShadow` | `none` | CSS text-shadow |
| `bgColor` | `transparent` locally, `#000` on `live.rctranslation.org` | Background color. Explicit value always wins. |
| `padding` | `20` | Container padding in px |
| `maxLines` | `0` (unlimited) | Max lines displayed (hard cap 200) |

## Cloudflare Tunnel (Internet Access)

To make the web display accessible over the internet (e.g. at `live.rctranslation.org`):

```bash
# One-time setup
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create church-live
cloudflared tunnel route dns church-live live.rctranslation.org
```

The tunnel starts automatically when you run the script — `--tunnel church-live` is the default. Pass `--no-tunnel` to skip it for local-only work:

```bash
python soniox_claude.py              # tunnel runs automatically
python soniox_claude.py --no-tunnel  # localhost only
```

Viewers can access:
- `https://live.rctranslation.org/` — source-language transcriptions with a solid black background (default)
- `https://live.rctranslation.org/?mode=translation&lang=en` — English translations

### Waiting page

When the tunnel has no origin (i.e. no device is running `soniox_claude.py`), visitors to `live.rctranslation.org` see Cloudflare's default 530 error. To replace that with a branded "Waiting for transcription…" page that auto-refreshes into captions when the tunnel comes back online, deploy the Cloudflare Worker in `worker/`. See [`worker/README.md`](worker/README.md) for the one-time deploy.

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--lang {ko,en}` | `ko` | Source language: `ko` = Korean→English, `en` = English→Korean |
| `--device N` | (interactive) | Audio input device index (skip selection prompt) |
| `--port PORT` | `8080` | Web caption server port (`0` to disable) |
| `--tunnel NAME` | `church-live` | Cloudflare tunnel name to start |
| `--no-tunnel` | — | Skip starting the Cloudflare tunnel |
| `--outline PATH` | — | Path to a UTF-8 `.txt` sermon outline. Enables prompt caching when the combined system prompt exceeds 1024 tokens. |

## License

Unlicense
