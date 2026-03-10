#!/usr/bin/env python3
"""
OpenWebUI Monitor - Model Price Sync Script

Scrapes current API pricing from OpenAI and Anthropic official pages,
then updates an OpenWebUI Monitor instance via its REST API.

Configuration is loaded from a JSON config file (default: config.json).
See config.example.json for the template.

Usage:
    python3 sync.py
    python3 sync.py --dry-run
    python3 sync.py --config /path/to/config.json
    python3 sync.py --source litellm

Dependencies:
    pip install requests beautifulsoup4
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import requests
from bs4 import BeautifulSoup

__version__ = "1.0.0"

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "monitor_url": "",
    "monitor_token": "",
    "source": "both",
    "threshold": 0.001,
    "free_model_patterns": [
        ":latest",
        "phi4-mini",
        "sora-2",
    ],
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
}


def load_config(config_path):
    """
    Load configuration from a JSON file, falling back to env vars and defaults.
    Returns a dict with all config keys populated.
    """
    config = dict(DEFAULT_CONFIG)

    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            file_config = json.load(f)
        # Merge file config over defaults (only known keys)
        for key in DEFAULT_CONFIG:
            if key in file_config:
                config[key] = file_config[key]
        print(f"  Loaded config from {config_path}")
    else:
        print(f"  Config file not found: {config_path} (using env vars / defaults)")

    # Env vars override config file
    if os.environ.get("MONITOR_URL"):
        config["monitor_url"] = os.environ["MONITOR_URL"]
    if os.environ.get("MONITOR_TOKEN"):
        config["monitor_token"] = os.environ["MONITOR_TOKEN"]

    return config


# ─── Price Scraping: OpenAI ──────────────────────────────────────────────────


def parse_price(text):
    """Extract a numeric dollar price from a string like '$2.50' or '-'."""
    text = text.strip().replace(",", "")
    match = re.search(r"\$?([\d.]+)", text)
    if match:
        return float(match.group(1))
    return None


def _parse_openai_table(table):
    """Parse a single OpenAI pricing table into a dict of model prices."""
    rows = table.find_all("tr")
    if not rows:
        return {}

    header_cells = [th.get_text(strip=True).lower() for th in rows[0].find_all(["th", "td"])]

    # We want tables with Model, Input, Output columns
    # Skip fine-tuning (Training), image-per-pixel (Quality), tool pricing (Cost), etc.
    if "model" not in header_cells:
        return {}
    if "input" not in header_cells or "output" not in header_cells:
        return {}
    if "training" in header_cells or "quality" in header_cells or "cost" in header_cells:
        return {}

    input_idx = header_cells.index("input")
    output_idx = header_cells.index("output")

    result = {}
    for row in rows[1:]:
        cells = row.find_all(["th", "td"])
        if len(cells) <= max(input_idx, output_idx):
            continue

        model_name = cells[0].get_text(strip=True).lower()
        model_name = re.sub(r"\s*\(.*?\)\s*", "", model_name).strip()

        if not model_name or model_name == "model":
            continue

        input_price = parse_price(cells[input_idx].get_text(strip=True))
        output_price = parse_price(cells[output_idx].get_text(strip=True))

        if input_price is None and output_price is None:
            continue

        # Keep first occurrence only (standard/short context, not long-context surcharge)
        if model_name not in result:
            result[model_name] = {
                "input_price": input_price or 0,
                "output_price": output_price or 0,
            }

    return result


def scrape_openai_prices():
    """
    Scrape the official OpenAI API pricing page (Standard tier).
    Returns dict of { model_id: { input_price, output_price } }
    Prices are per 1M tokens.

    The page contains multiple tables for different pricing tiers
    (Batch, Flex, Standard, Priority). We select the Standard tier
    by picking the largest text-token table (most model rows).
    """
    print("  Fetching OpenAI pricing page...")
    url = "https://developers.openai.com/api/docs/pricing"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tables = soup.find_all("table")

    # Parse all candidate pricing tables
    candidates = []
    for table in tables:
        parsed = _parse_openai_table(table)
        if parsed:
            candidates.append(parsed)

    if not candidates:
        print("  WARNING: No pricing tables found on OpenAI page")
        return {}

    # The Standard tier table is the one with the most models.
    # Page layout: Batch → Flex → Standard → Priority
    # Standard has the most complete listing.
    standard_table = max(candidates, key=len)

    # Also grab the Legacy models table (second-to-last matching table)
    # and any smaller specialty tables (audio, transcription, etc.)
    prices = dict(standard_table)

    # Merge in other tables for models not in the main table
    # (e.g., legacy models, audio/transcription in separate tables)
    for table_data in candidates:
        if table_data is standard_table:
            continue
        for model_id, price_info in table_data.items():
            if model_id not in prices:
                prices[model_id] = price_info

    print(f"  Found {len(prices)} OpenAI models with pricing")
    return prices


# ─── Price Scraping: Anthropic ───────────────────────────────────────────────


def scrape_anthropic_prices():
    """
    Scrape the Anthropic API pricing from their docs.
    Returns dict of { model_id: { input_price, output_price } }
    Prices are per 1M tokens.

    The Anthropic models page uses a transposed table layout where
    models are columns and features are rows. The pricing row contains
    text like "$5 / input MTok$25 / output MTok".
    """
    print("  Fetching Anthropic pricing page...")
    url = "https://docs.anthropic.com/en/docs/about-claude/models"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    prices = {}

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        # Build a dict of row_label -> [col1_value, col2_value, ...]
        row_data = {}
        for row in rows:
            cells = [td.get_text(strip=True) for td in row.find_all(["th", "td"])]
            if cells:
                row_data[cells[0].lower()] = cells[1:]

        # Find the model ID row
        model_ids = []
        for key in ("claude api id", "api model name"):
            if key in row_data:
                model_ids = row_data[key]
                break

        if not model_ids:
            continue

        # Find the pricing row (might be "Pricing", "Pricing1", etc.)
        pricing_cells = []
        for key, values in row_data.items():
            if key.startswith("pricing"):
                pricing_cells = values
                break

        if not pricing_cells:
            continue

        # Parse each model's pricing
        for i, model_id in enumerate(model_ids):
            if i >= len(pricing_cells):
                break

            price_text = pricing_cells[i]
            # Parse "$5 / input MTok$25 / output MTok" format
            price_matches = re.findall(r"\$(\d+(?:\.\d+)?)", price_text)
            if len(price_matches) >= 2:
                input_price = float(price_matches[0])
                output_price = float(price_matches[1])
                prices[model_id.lower().strip()] = {
                    "input_price": input_price,
                    "output_price": output_price,
                }

    print(f"  Found {len(prices)} Anthropic models with pricing")
    return prices


# ─── Fallback: LiteLLM Pricing Data ─────────────────────────────────────────


def fetch_litellm_prices():
    """
    Fetch pricing from LiteLLM's community-maintained pricing database.
    Returns dict of { model_id: { input_price, output_price } }
    Prices are per 1M tokens.
    """
    print("  Fetching LiteLLM pricing database...")
    url = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    prices = {}
    for model_id, info in data.items():
        if "sample_spec" in model_id:
            continue

        input_cost = info.get("input_cost_per_token")
        output_cost = info.get("output_cost_per_token")

        if input_cost is None or output_cost is None:
            continue

        # Convert from per-token to per-1M-tokens
        prices[model_id] = {
            "input_price": round(input_cost * 1_000_000, 4),
            "output_price": round(output_cost * 1_000_000, 4),
        }

    print(f"  Found {len(prices)} models in LiteLLM database")
    return prices


# ─── Monitor API ─────────────────────────────────────────────────────────────


def get_monitor_models(monitor_url, monitor_token):
    """Fetch all models currently configured in OpenWebUI Monitor."""
    resp = requests.get(
        f"{monitor_url}/api/v1/models",
        headers={"Authorization": f"Bearer {monitor_token}"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def update_monitor_prices(monitor_url, monitor_token, updates):
    """
    Push price updates to OpenWebUI Monitor.
    updates: list of { id, input_price, output_price, per_msg_price }
    """
    if not updates:
        print("  No updates to push.")
        return

    resp = requests.post(
        f"{monitor_url}/api/v1/models/price",
        headers={
            "Authorization": f"Bearer {monitor_token}",
            "Content-Type": "application/json",
        },
        json=updates,
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"  API response: {result.get('message', 'OK')}")
    return result


# ─── Matching Logic ──────────────────────────────────────────────────────────


def is_free_model(model_id, free_patterns):
    """Check if a model should be priced at 0 (local/free)."""
    for pattern in free_patterns:
        if pattern in model_id.lower():
            return True
    return False


def find_price(model_id, openai_prices, anthropic_prices, litellm_prices, free_patterns):
    """
    Look up the price for a given model ID across all sources.
    Returns (input_price, output_price) or None if not found.
    """
    mid = model_id.lower().strip()

    # Check free models first
    if is_free_model(mid, free_patterns):
        return (0, 0)

    # Direct match in OpenAI prices
    if mid in openai_prices:
        p = openai_prices[mid]
        return (p["input_price"], p["output_price"])

    # Direct match in Anthropic prices
    if mid in anthropic_prices:
        p = anthropic_prices[mid]
        return (p["input_price"], p["output_price"])

    # Strip date suffixes for matching (e.g., "gpt-5-2025-08-07" -> "gpt-5")
    # Try progressively shorter versions
    parts = mid.split("-")
    for i in range(len(parts), 0, -1):
        candidate = "-".join(parts[:i])
        # Skip if we've reduced to just "gpt" or "claude"
        if candidate in ("gpt", "claude"):
            break

        if candidate in openai_prices:
            p = openai_prices[candidate]
            return (p["input_price"], p["output_price"])
        if candidate in anthropic_prices:
            p = anthropic_prices[candidate]
            return (p["input_price"], p["output_price"])

    # LiteLLM fallback — try with and without provider prefix
    for prefix in ["", "openai/", "anthropic/"]:
        key = prefix + mid
        if key in litellm_prices:
            p = litellm_prices[key]
            return (p["input_price"], p["output_price"])

    # LiteLLM with date stripping
    for i in range(len(parts), 0, -1):
        candidate = "-".join(parts[:i])
        if candidate in ("gpt", "claude"):
            break
        for prefix in ["", "openai/", "anthropic/"]:
            key = prefix + candidate
            if key in litellm_prices:
                p = litellm_prices[key]
                return (p["input_price"], p["output_price"])

    return None


# ─── Main ────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Sync model prices to OpenWebUI Monitor from official API pricing pages."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="Path to JSON config file (default: config.json).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without actually updating.",
    )
    parser.add_argument(
        "--source",
        choices=["scrape", "litellm", "both"],
        default=None,
        help="Price source: 'scrape' official pages, 'litellm' database, or 'both' (default).",
    )
    parser.add_argument(
        "--monitor-url",
        default=None,
        help="OpenWebUI Monitor URL (overrides config file and env var).",
    )
    parser.add_argument(
        "--monitor-token",
        default=None,
        help="OpenWebUI Monitor access token (overrides config file and env var).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Minimum price difference to trigger an update (default: 0.001).",
    )
    args = parser.parse_args()

    # Load config: defaults < config file < env vars < CLI args
    config = load_config(args.config)

    if args.monitor_url:
        config["monitor_url"] = args.monitor_url
    if args.monitor_token:
        config["monitor_token"] = args.monitor_token
    if args.source:
        config["source"] = args.source
    if args.threshold is not None:
        config["threshold"] = args.threshold

    monitor_url = config["monitor_url"]
    monitor_token = config["monitor_token"]
    source = config["source"]
    threshold = config["threshold"]
    free_patterns = config["free_model_patterns"]

    if not monitor_token:
        print("ERROR: No monitor token provided.")
        print(
            "Set monitor_token in config.json, MONITOR_TOKEN env var, or use --monitor-token flag."
        )
        sys.exit(1)

    if not monitor_url:
        print("ERROR: No monitor URL provided.")
        print("Set monitor_url in config.json, MONITOR_URL env var, or use --monitor-url flag.")
        sys.exit(1)

    print(f"{'=' * 60}")
    print(f"OpenWebUI Monitor Price Sync v{__version__}")
    print(f"Monitor: {monitor_url}")
    print(f"Time:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Source:  {source}")
    print(f"{'=' * 60}")

    # Step 1: Fetch pricing data from sources
    print("\n[1/4] Fetching pricing data...")
    openai_prices = {}
    anthropic_prices = {}
    litellm_prices = {}

    if source in ("scrape", "both"):
        try:
            openai_prices = scrape_openai_prices()
        except Exception as e:
            print(f"  WARNING: Failed to scrape OpenAI: {e}")
        try:
            anthropic_prices = scrape_anthropic_prices()
        except Exception as e:
            print(f"  WARNING: Failed to scrape Anthropic: {e}")

    if source in ("litellm", "both"):
        try:
            litellm_prices = fetch_litellm_prices()
        except Exception as e:
            print(f"  WARNING: Failed to fetch LiteLLM data: {e}")

    if not openai_prices and not anthropic_prices and not litellm_prices:
        print("\nERROR: No pricing data available from any source.")
        sys.exit(1)

    # Step 2: Fetch current models from Monitor
    print("\n[2/4] Fetching current models from Monitor...")
    try:
        monitor_models = get_monitor_models(monitor_url, monitor_token)
        print(f"  Found {len(monitor_models)} models in Monitor")
    except Exception as e:
        print(f"  ERROR: Failed to fetch models: {e}")
        sys.exit(1)

    # Step 3: Compare and build update list
    print("\n[3/4] Comparing prices...")
    updates = []
    unchanged = []
    not_found = []

    for model in monitor_models:
        mid = model["id"]
        current_input = model.get("input_price", 60)
        current_output = model.get("output_price", 60)
        current_per_msg = model.get("per_msg_price", -1)

        result = find_price(mid, openai_prices, anthropic_prices, litellm_prices, free_patterns)

        if result is None:
            not_found.append(mid)
            continue

        new_input, new_output = result

        # Check if price actually changed
        input_diff = abs(current_input - new_input)
        output_diff = abs(current_output - new_output)

        if input_diff > threshold or output_diff > threshold:
            updates.append(
                {
                    "id": mid,
                    "input_price": new_input,
                    "output_price": new_output,
                    "per_msg_price": current_per_msg if current_per_msg != -1 else -1,
                }
            )
            direction_in = (
                "↑" if new_input > current_input else "↓" if new_input < current_input else "="
            )
            direction_out = (
                "↑" if new_output > current_output else "↓" if new_output < current_output else "="
            )
            print(
                f"  UPDATE {mid}: "
                f"${current_input:.4f}→${new_input:.4f} {direction_in} / "
                f"${current_output:.4f}→${new_output:.4f} {direction_out}"
            )
        else:
            unchanged.append(mid)

    print(
        f"\n  Summary: {len(updates)} to update, {len(unchanged)} unchanged, {len(not_found)} not found"
    )

    if not_found:
        print(f"  Models without pricing data: {', '.join(not_found[:10])}")
        if len(not_found) > 10:
            print(f"    ... and {len(not_found) - 10} more")

    # Step 4: Push updates
    print(f"\n[4/4] {'DRY RUN - ' if args.dry_run else ''}Pushing updates...")
    if args.dry_run:
        print("  Dry run mode — no changes made.")
        if updates:
            print(f"  Would update {len(updates)} models.")
    else:
        if updates:
            try:
                update_monitor_prices(monitor_url, monitor_token, updates)
                print(f"  Successfully updated {len(updates)} models.")
            except Exception as e:
                print(f"  ERROR: Failed to update prices: {e}")
                sys.exit(1)
        else:
            print("  All prices are already up to date!")

    print(f"\n{'=' * 60}")
    print("Done!")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
