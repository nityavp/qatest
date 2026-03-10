"""
AI-powered copywriting analysis using Gemini.
Analyzes CTAs, microcopy, error messages, tone, and conversion optimization.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import google.generativeai as genai
from models import Finding, PageData, JourneyResult

COPY_PROMPT = """You are an expert UX copywriter and conversion rate optimizer reviewing a web page's copy.

Page URL: {url}
Page Title: {title}

CTA elements found on this page:
{ctas}

Visible text (first 5000 chars):
{visible_text}

Analyze the copy across these five dimensions:

1. CTA EFFECTIVENESS — Are buttons/links action-oriented? Specific vs generic ("Submit" vs "Get Started Free")? Do they create urgency or convey value?

2. MICROCOPY QUALITY — Are form labels clear? Helper texts useful? Placeholder text meaningful? Navigation labels intuitive?

3. ERROR MESSAGE HELPFULNESS — If any error messages or validation text is visible, are they actionable? Do they explain what went wrong and how to fix?

4. TONE CONSISTENCY — Is the voice consistent across the page? Does it match what you'd expect for this type of site?

5. PERSUASION / CONVERSION — Is the value proposition clear? Are there trust signals? Does the copy address user concerns? Any friction in the copy that would hurt conversions?

Rules:
- Only flag genuine, actionable copy issues.
- Be specific: quote the exact text that needs improvement and suggest better alternatives.
- If copy is good in a dimension, return zero issues for it.

Respond with ONLY this JSON (no markdown fences):
{{"issues": [
  {{
    "severity": "critical|high|medium|low",
    "title": "Short title",
    "description": "What is wrong with the copy",
    "location": "Which element/section",
    "impact": "How it affects users or conversions",
    "suggestion": "Specific rewrite or improvement"
  }}
]}}"""


JOURNEY_COPY_PROMPT = """You are an expert UX copywriter analyzing the copy across a user journey ({journey_type}: {journey_name}).

Below is the visible text at each step of the journey:

{steps_text}

Analyze the journey's copy holistically:
1. Is the microcopy guiding the user clearly through each step?
2. Are form labels and placeholders helpful?
3. Are success/error/confirmation messages clear and actionable?
4. Is the tone consistent throughout the flow?
5. Are there any copy-related friction points that could cause drop-off?

Rules:
- Quote specific text and suggest improvements.
- Reference which step the issue appears at.
- Only flag actionable issues.

Respond with ONLY this JSON (no markdown fences):
{{"issues": [
  {{
    "severity": "critical|high|medium|low",
    "title": "Short title",
    "description": "What is wrong",
    "location": "Step N — element/section",
    "impact": "How it affects the user journey",
    "suggestion": "Specific rewrite or improvement"
  }}
]}}"""


def _hash_short(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:8]


def _parse_findings(text: str, page_url: str, prefix: str) -> list[Finding]:
    """Parse Gemini's JSON response into Finding objects."""
    findings = []
    try:
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        data = json.loads(text.strip())
        issues = data.get("issues", []) if isinstance(data, dict) else data

        for i, issue in enumerate(issues):
            findings.append(Finding(
                id=f"copy-{prefix}-{_hash_short(page_url)}-{i}",
                category="copywriting",
                severity=issue.get("severity", "medium"),
                title=issue.get("title", "Copy Issue"),
                description=issue.get("description", ""),
                location=f"{page_url} — {issue.get('location', '')}",
                impact=issue.get("impact", ""),
                suggestion=issue.get("suggestion", ""),
                source="ai",
            ))
    except (json.JSONDecodeError, KeyError, IndexError):
        pass
    return findings


def analyze_page_copy_sync(page_data: PageData, api_key: str) -> list[Finding]:
    """Analyze copywriting on a single page using Gemini."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    ctas_text = "\n".join(
        f"- [{c.get('tag', '?')}] \"{c.get('text', '')}\"" + (f" (href: {c.get('href', '')})" if c.get("href") else "")
        for c in page_data.cta_elements[:20]
    ) or "(No CTA elements found)"

    prompt = COPY_PROMPT.format(
        url=page_data.url,
        title=page_data.title,
        ctas=ctas_text,
        visible_text=page_data.visible_text[:5000],
    )

    response = model.generate_content(prompt)
    return _parse_findings(response.text, page_data.url, "page")


def analyze_journey_copy_sync(journey: JourneyResult, api_key: str) -> list[Finding]:
    """Analyze copywriting across a user journey's steps."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    steps_text = ""
    for step in journey.steps:
        steps_text += f"\n--- Step {step.step_number}: {step.description} ---\n"
        steps_text += f"URL: {step.url_after}\n"
        steps_text += f"Visible text:\n{step.visible_text[:2000]}\n"

    prompt = JOURNEY_COPY_PROMPT.format(
        journey_type=journey.journey_type,
        journey_name=journey.journey_name,
        steps_text=steps_text[:8000],
    )

    response = model.generate_content(prompt)
    return _parse_findings(response.text, journey.start_url, f"journey-{journey.journey_type}")


async def run_copy_analysis(
    site_data,
    journey_results: list[JourneyResult],
    api_key: str,
    on_progress=None,
) -> list[Finding]:
    """Run copywriting analysis on all pages and journey flows."""
    all_findings = []

    # Analyze each page's copy
    for i, page in enumerate(site_data.pages):
        if not page.visible_text.strip():
            continue
        if on_progress:
            on_progress(f"  Copywriting analysis page {i+1}/{len(site_data.pages)}: {page.url}")
        try:
            findings = await asyncio.to_thread(analyze_page_copy_sync, page, api_key)
            all_findings.extend(findings)
        except Exception as e:
            if on_progress:
                on_progress(f"    Copy analysis error: {e}")

    # Analyze each journey's copy
    for journey in journey_results:
        if len(journey.steps) < 2:
            continue
        if on_progress:
            on_progress(f"  Copywriting analysis: {journey.journey_name} journey")
        try:
            findings = await asyncio.to_thread(analyze_journey_copy_sync, journey, api_key)
            all_findings.extend(findings)
        except Exception as e:
            if on_progress:
                on_progress(f"    Journey copy analysis error: {e}")

    return all_findings
