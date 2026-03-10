#!/usr/bin/env python3
"""
QATest v2 — AI-powered end-to-end QA testing from a single URL.

Usage:
    python3 main.py https://example.com
    python3 main.py https://example.com --email test@test.com --password pass123
    python3 main.py https://example.com --baseline path/to/baseline.json
    python3 main.py https://example.com --max-pages 20 --output ./my-report
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
from journey import run_journeys
from copywriter import run_copy_analysis
from report import calculate_scores, generate_report
from models import Finding


def log(msg):
    print(f"  {msg}")


async def run(args):
    url = args.url
    if not url.startswith("http"):
        url = "https://" + url

    api_key = os.environ.get("GEMINI_API_KEY", "")

    print()
    print(f"  QATest v2 — AI-Powered QA Report")
    print(f"  {'='*40}")
    print(f"  Target: {url}")
    print(f"  Max pages: {args.max_pages}")
    if args.email:
        print(f"  Journey creds: {args.email}")
    print()

    # Step 1: Crawl
    print("[1/6] Crawling site...")
    site_data = await crawl_site(url, max_pages=args.max_pages, on_progress=log)
    print()
    if not site_data.pages:
        print("ERROR: Could not crawl any pages.")
        sys.exit(1)

    # Step 2: Analysis
    print("[2/6] Running analysis...")
    findings = await run_analysis(site_data, api_key=api_key, on_progress=log)
    print(f"  Found {len(findings)} issue(s)")
    print()

    # Step 3: User journeys
    print("[3/6] Testing user journeys...")
    journey_results = []
    if api_key:
        journey_results = await run_journeys(
            site_data, api_key, args.email or "", args.password or "", on_progress=log
        )
        for jr in journey_results:
            if not jr.overall_success:
                failed = next((s for s in jr.steps if not s.success), None)
                findings.append(Finding(
                    id=f"journey-fail-{jr.journey_type}",
                    category="journey", severity="critical",
                    title=f"{jr.journey_name} — Journey Failed",
                    description=f"Failed at step {failed.step_number}: {failed.error_message}" if failed else "Journey could not be completed.",
                    location=jr.start_url, impact=f"Users cannot complete {jr.journey_type}.",
                    suggestion="Fix the failing step.", source="journey",
                ))
    else:
        print("  Skipping (no GEMINI_API_KEY)")
    print()

    # Step 4: Copywriting
    print("[4/6] Analyzing copywriting...")
    if api_key:
        copy_findings = await run_copy_analysis(site_data, journey_results, api_key, on_progress=log)
        findings.extend(copy_findings)
        print(f"  Copywriting: {len(copy_findings)} issue(s)")
    else:
        print("  Skipping (no API key)")
    print()

    # Step 5: Score
    print("[5/6] Calculating scores...")
    scoring_findings = [f for f in findings if not f.id.startswith("journey-pass-")]
    scores = calculate_scores(scoring_findings)
    print(f"  Product Readiness Score: {scores['overall']}/100")
    for cat, info in scores["categories"].items():
        print(f"    {cat.title():15s} {info['score']:3d}/100")
    print()

    # Step 6: Report
    print("[6/6] Generating report...")
    baseline = None
    if args.baseline:
        try:
            with open(args.baseline) as f:
                baseline = json.load(f)
            print(f"  Loaded baseline from {args.baseline}")
        except Exception as e:
            print(f"  Warning: Could not load baseline: {e}")

    if args.output:
        output_dir = args.output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        domain = url.replace("https://", "").replace("http://", "").split("/")[0].replace(".", "_")
        output_dir = os.path.join(os.getcwd(), f"qatest_report_{domain}_{ts}")

    report_path = generate_report(site_data, findings, scores, output_dir, baseline, journey_results)

    print()
    print(f"  Report: {report_path}")
    print(f"  Baseline: {os.path.join(output_dir, 'baseline.json')}")
    print()


def main():
    parser = argparse.ArgumentParser(description="QATest v2 — AI-powered QA testing")
    parser.add_argument("url", help="URL to test")
    parser.add_argument("--max-pages", type=int, default=10, help="Max pages (default: 10)")
    parser.add_argument("--email", help="Email for login/signup journey testing")
    parser.add_argument("--password", help="Password for login/signup journey testing")
    parser.add_argument("--baseline", help="Path to previous baseline.json for Run 2")
    parser.add_argument("--output", help="Output directory")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
