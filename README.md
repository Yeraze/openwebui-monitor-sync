# openwebui-monitor-sync

Automatically syncs AI model pricing data to an [OpenWebUI Monitor](https://github.com/VariantConst/OpenWebUI-Monitor) instance. Scrapes current prices from provider websites and updates your Monitor so usage costs stay accurate.

Intended to be run on a schedule (daily or weekly) via cron, systemd timer, or similar.

## Supported Providers

Pricing is scraped directly from official sources:

- **OpenAI** — scraped from the [API pricing page](https://developers.openai.com/api/docs/pricing) (Standard tier)
- **Anthropic** — scraped from the [models documentation](https://docs.anthropic.com/en/docs/about-claude/models)
- **LiteLLM** (fallback) — [community-maintained pricing database](https://github.com/BerriAI/litellm) covering 100+ providers (Google, Mistral, Cohere, and many more)

Models matching configured free patterns (e.g. Ollama local models) are always priced at $0.

## Prerequisites

- Python 3.8+
- A running [OpenWebUI Monitor](https://github.com/VariantConst/OpenWebUI-Monitor) instance
- An API token for your Monitor instance

## Installation

```bash
git clone https://github.com/yeraze/openwebui-monitor-sync.git
cd openwebui-monitor-sync
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example config and fill in your values:

```bash
cp config.example.json config.json
```

Edit `config.json`:

```json
{
    "monitor_url": "https://your-monitor-instance.example.com",
    "monitor_token": "your-access-token-here",
    "source": "both",
    "threshold": 0.001,
    "free_model_patterns": [
        ":latest",
        "phi4-mini",
        "sora-2"
    ]
}
```

| Field | Description |
|---|---|
| `monitor_url` | URL of your OpenWebUI Monitor instance |
| `monitor_token` | API access token for your Monitor instance |
| `source` | Price source: `scrape` (official pages only), `litellm` (LiteLLM database only), or `both` (default) |
| `threshold` | Minimum price difference (per 1M tokens) to trigger an update. Default: `0.001` |
| `free_model_patterns` | List of substrings — any model ID containing one of these is priced at $0/$0 |

Environment variables `MONITOR_URL` and `MONITOR_TOKEN` override the config file. CLI flags override everything.

## Usage

```bash
# Run with default config.json
python3 sync.py

# Dry run — show what would change without updating
python3 sync.py --dry-run

# Use a different config file
python3 sync.py --config /etc/openwebui-monitor-sync/config.json

# Override source from CLI
python3 sync.py --source litellm
```

### CLI Options

| Flag | Description |
|---|---|
| `--config PATH` | Path to config file (default: `config.json`) |
| `--dry-run` | Show changes without pushing updates |
| `--source {scrape,litellm,both}` | Override price source |
| `--monitor-url URL` | Override Monitor URL |
| `--monitor-token TOKEN` | Override Monitor token |
| `--threshold N` | Override minimum price difference |

## Scheduling with Cron

Run weekly on Sundays at 3 AM:

```bash
crontab -e
```

```cron
0 3 * * 0 cd /path/to/openwebui-monitor-sync && .venv/bin/python3 sync.py >> /var/log/openwebui-monitor-sync.log 2>&1
```

Or daily at midnight:

```cron
0 0 * * * cd /path/to/openwebui-monitor-sync && .venv/bin/python3 sync.py >> /var/log/openwebui-monitor-sync.log 2>&1
```

## How It Works

1. **Scrapes prices** from OpenAI and Anthropic official pages, and/or fetches from LiteLLM's pricing database
2. **Fetches models** currently configured in your OpenWebUI Monitor instance
3. **Matches and diffs** — finds each model's current price in the scraped data, using fuzzy matching (date-suffix stripping, provider prefix variants)
4. **Pushes updates** for any models whose prices changed beyond the threshold

All prices are in **USD per 1M tokens**.

## License

MIT
