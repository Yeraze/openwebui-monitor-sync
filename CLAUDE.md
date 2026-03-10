# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file Python script (`sync.py`) that scrapes current API pricing from OpenAI and Anthropic official pages, then updates an [OpenWebUI Monitor](https://github.com/VariantConst/OpenWebUI-Monitor) instance via its REST API. LiteLLM's pricing database is used as a fallback source. Intended to be run on a schedule (cron) daily or weekly.

## Running

```bash
pip install requests beautifulsoup4
cp config.example.json config.json   # then edit with your monitor_url and monitor_token
python3 sync.py
python3 sync.py --dry-run
python3 sync.py --config /other/path/config.json
```

CLI flags: `--config`, `--source {scrape,litellm,both}`, `--monitor-url`, `--monitor-token`, `--threshold`, `--dry-run`.

## Configuration

Config is loaded from `config.json` (or path specified by `--config`). Priority: CLI args > env vars (`MONITOR_URL`, `MONITOR_TOKEN`) > config file > defaults. The config file is gitignored since it contains secrets; `config.example.json` is the checked-in template.

## Architecture

All logic is in `sync.py`. The flow is:

1. **Load config** — `load_config()` merges defaults, config file, and env vars
2. **Scrape prices** — `scrape_openai_prices()` parses HTML tables from OpenAI's pricing page; `scrape_anthropic_prices()` handles Anthropic's transposed table layout; `fetch_litellm_prices()` pulls from LiteLLM's JSON on GitHub
3. **Fetch monitor models** — `get_monitor_models()` calls the Monitor REST API
4. **Match & diff** — `find_price()` matches each monitor model against scraped prices using direct match, date-suffix stripping, and LiteLLM prefix variants
5. **Push updates** — `update_monitor_prices()` POSTs changed prices back to Monitor

All prices are in **dollars per 1M tokens**. Models matching `free_model_patterns` (from config) are always priced at 0/0.

## Key Details

- OpenAI scraper selects the "Standard tier" table by picking the largest table (most rows)
- Anthropic's page uses a transposed layout (models as columns, features as rows)
- Model matching strips date suffixes progressively (e.g., `gpt-5-2025-08-07` -> `gpt-5`)
- LiteLLM prices are converted from per-token to per-1M-tokens
- No global mutable state — monitor URL/token are passed as function parameters
