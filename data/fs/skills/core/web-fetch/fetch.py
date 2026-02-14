#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp", "tqdm"]
# ///

import asyncio
import os
import sys
import argparse
from typing import List, Dict

async def fetch_url(session: aiohttp.ClientSession, target: str, jina_key: str, use_browser: bool = True) -> str:
    url = f"https://r.jina.ai/{target}"
    headers = {
        "Authorization": f"Bearer {jina_key}",
        "Accept": "text/plain",
    }
    if use_browser:
        headers["X-Experimental"] = "browser"

    try:
        async with session.get(url, headers=headers, timeout=60) as response:
            if response.status == 200:
                return await response.text()
            elif use_browser:
                # Fallback
                return await fetch_url(session, target, jina_key, use_browser=False)
            else:
                return f"Error: {response.status} for {target}"
    except Exception as e:
        return f"Error: {str(e)} for {target}"

async def main():
    parser = argparse.ArgumentParser(description="Fetch one or more URLs via Jina Reader")
    parser.add_argument("urls", nargs="+", help="URLs to fetch")
    parser.add_argument("--out-dir", default="/tmp/web-fetch", help="Output directory")
    args = parser.parse_args()

    jina_key = os.environ.get("JINA_AI_KEY")
    if not jina_key:
        print("Error: JINA_AI_KEY environment variable not set.")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_url(session, url, jina_key) for url in args.urls]
        results = await asyncio.gather(*tasks)

        for i, (url, content) in enumerate(zip(args.urls, results)):
            filename = os.path.join(args.out_dir, f"fetch_{i}.txt")
            with open(filename, "w") as f:
                f.write(f"URL: {url}\n\n{content}")
            print(f"Saved {url} to {filename} (Preview: {content[:100].replace('\n', ' ')}...)")

if __name__ == "__main__":
    # Fix for aiohttp.ClientSession in newer versions
    import aiohttp
    asyncio.run(main())
