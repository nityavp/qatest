#!/usr/bin/env python3
"""
QATest — AI-powered end-to-end QA testing from a single URL.

Usage:
    # Run 1 (Baseline)
    python main.py https://example.com

    # Run 2 (Verification — compares against previous baseline)
    python main.py https://example.com --baseline path/to/baseline.json

    # Options
    python main.py https://example.com --max-pages 20 --output ./my-report
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

from crawler import crawl_site
from analyzer import run_analysis
from report import calculate_scores, generate_report


def log(msg):
    print(f"  {msg}")


async def run(args):
    url = args.url
    if not url.startswith("http"):
        url = "https://" + url

    print()
    print(f"  QATest — AI-Powered QA Report")
    print(f"  {'='*40}")
    print(f"  Target: {url}")
    print(f"  Max pages: {args.max_pages}")
    print()

    # ── Step 1: Crawl ────────────────────────────────────────────
    print("[1/4] Crawling site...")
    site_data = await crawl_site(url, max_pages=args.max_pages, on_progress=log)
    print()

    if not site_data.pages:
        print("ERROR: Could not crawl any pages. Check the URL and try again.")
        sys.exit(1)

    # ── Step 2: Analyse ──────────────────────────────────────────
    print("[2/4] Running analysis...")
    api_key = os.environ.get("GEMINI_API_KEY", "")
    findings = await run_analysis(site_data, api_key=api_key, on_progress=log)
    print(f"  Found {len(findings)} issue(s)")
    print()

    # ── Step 3: Score ────────────────────────────────────────────
    print("[3/4] Calculating scores...")
    scores = calculate_scores(findings)
    print(f"  Product Readiness Score: {scores['overall']}/100")
    for cat, info in scores["categories"].items():
        print(f"    {cat.title():15s} {info['score']:3d}/100")
    print()

    # ── Step 4: Report ───────────────────────────────────────────
    print("[4/4] Generating report...")

    # Load baseline if provided (Run 2)
    baseline = None
    if args.baseline:
        try:
            with open(args.baseline) as f:
                baseline = json.load(f)
            print(f"  Loaded baseline from {args.baseline}")
            print(f"  Previous score: {baseline.get('overall_score', '?')}/100")
        except Exception as e:
            print(f"  Warning: Could not load baseline: {e}")

    # Determine output directory
    if args.output:
        output_dir = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        domain = url.replace("https://", "").replace("http://", "").split("/")[0].replace(".", "_")
        output_dir = os.path.join(os.getcwd(), f"qatest_report_{domain}_{ts}")

    report_path = generate_report(site_data, findings, scores, output_dir, baseline)

    print()
    print(f"  Report saved to: {report_path}")
    print(f"  Baseline JSON:   {os.path.join(output_dir, 'baseline.json')}")
    print()
    print(f"  For Run 2, use:")
    print(f"    python main.py {url} --baseline {os.path.join(output_dir, 'baseline.json')}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="QATest — AI-powered QA testing from a URL"
    )
    parser.add_argument("url", help="URL to test (e.g. https://example.com)")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max pages to crawl (default: 10)",
    )
    parser.add_argument(
        "--baseline",
        help="Path to a previous baseline.json for Run 2 comparison",
    )
    parser.add_argument(
        "--output",
        help="Output directory for the report",
    )

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
