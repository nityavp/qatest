"""
Journey detection, planning (via Gemini), and execution (via Playwright).
"""
from __future__ import annotations
import asyncio
import json
import time
import google.generativeai as genai
from playwright.async_api import async_playwright
from models import (
    Finding, SiteData, PageData,
    JourneyPlan, JourneyStep, JourneyResult,
)

# ── Heuristic journey detection ─────────────────────────────────────

JOURNEY_PATTERNS = {
    "login": {
        "password_required": True,
        "field_hints": ["email", "username", "user", "login", "phone"],
        "url_hints": ["login", "signin", "sign-in", "auth", "session"],
        "button_hints": ["log in", "login", "sign in", "signin", "submit"],
        "max_fields": 5,
        "label": "Login Flow",
    },
    "signup": {
        "password_required": True,
        "field_hints": ["email", "name", "first", "last", "confirm", "phone"],
        "url_hints": ["register", "signup", "sign-up", "join", "create"],
        "button_hints": ["sign up", "signup", "register", "create account", "join", "get started"],
        "min_fields": 3,
        "label": "Sign Up Flow",
    },
    "contact": {
        "password_required": False,
        "field_hints": ["message", "comment", "body", "inquiry", "subject", "name", "email"],
        "url_hints": ["contact", "feedback", "inquiry", "support", "message"],
        "button_hints": ["send", "submit", "contact", "message"],
        "has_textarea": True,
        "label": "Contact Form",
    },
    "search": {
        "password_required": False,
        "field_hints": ["search", "query", "q", "keyword", "find"],
        "url_hints": ["search"],
        "button_hints": ["search", "find", "go"],
        "max_fields": 2,
        "label": "Search",
    },
    "newsletter": {
        "password_required": False,
        "field_hints": ["email"],
        "url_hints": ["subscribe", "newsletter", "mailing"],
        "button_hints": ["subscribe", "sign up", "join", "submit"],
        "max_fields": 2,
        "label": "Newsletter Subscription",
    },
}


def detect_journeys(site_data: SiteData) -> list[dict]:
    """
    Scan crawled pages for forms that match known journey patterns.
    Returns a list of candidate dicts with journey_type, page, form, score.
    """
    candidates = []

    for page in site_data.pages:
        for form_idx, form in enumerate(page.forms):
            fields = form.get("fields", [])
            buttons = form.get("buttons", [])
            field_names = " ".join(
                f.get("name", "") + " " + f.get("placeholder", "") + " " + f.get("id", "")
                for f in fields
            ).lower()
            field_types = [f.get("type", "") for f in fields]
            button_text = " ".join(b.get("text", "") for b in buttons).lower()
            url_lower = page.url.lower()
            form_action = (form.get("action", "") or "").lower()
            has_password = "password" in field_types
            has_textarea = "textarea" in field_types

            for jtype, pattern in JOURNEY_PATTERNS.items():
                score = 0

                # Password check
                if pattern.get("password_required") and not has_password:
                    continue
                if has_password and pattern.get("password_required"):
                    score += 3

                # Textarea check
                if pattern.get("has_textarea") and has_textarea:
                    score += 2

                # Field name hints
                for hint in pattern.get("field_hints", []):
                    if hint in field_names:
                        score += 1

                # URL hints
                for hint in pattern.get("url_hints", []):
                    if hint in url_lower or hint in form_action:
                        score += 2

                # Button text hints
                for hint in pattern.get("button_hints", []):
                    if hint in button_text:
                        score += 2

                # Field count constraints
                if "max_fields" in pattern and len(fields) > pattern["max_fields"]:
                    score -= 2
                if "min_fields" in pattern and len(fields) < pattern["min_fields"]:
                    score -= 2

                # Differentiate login vs signup when both have password
                if jtype == "login" and len(fields) > 4:
                    score -= 1
                if jtype == "signup" and len(fields) <= 2:
                    score -= 1

                if score >= 3:
                    candidates.append({
                        "journey_type": jtype,
                        "score": score,
                        "page_url": page.url,
                        "form_index": form_idx,
                        "form": form,
                        "label": pattern["label"],
                    })

    # Deduplicate: keep highest-scoring candidate per (form, page)
    best = {}
    for c in candidates:
        key = (c["page_url"], c["form_index"])
        if key not in best or c["score"] > best[key]["score"]:
            best[key] = c

    return sorted(best.values(), key=lambda x: -x["score"])[:5]


# ── Gemini-powered journey planning ─────────────────────────────────

PLAN_PROMPT = """You are a QA engineer creating a step-by-step test plan for a web form.

Form type: {journey_type} ({label})
Page URL: {url}
Form details:
{form_json}

User credentials available: email = "{email}", password = "{password}"

Generate an execution plan as JSON. Each step should be one of:
- fill: Fill a form field
- click: Click a button or link
- wait: Wait for page change

For "fill" steps, use the actual credential values where appropriate (email for email fields, password for password fields). For other fields (name, message, etc.) use realistic test data.

IMPORTANT: For selectors, prefer this priority order:
1. #id (if the element has an id)
2. [name="fieldname"] (if it has a name)
3. [placeholder="..."] (if it has a placeholder)
4. CSS class selector as last resort

Respond with ONLY this JSON (no markdown):
{{"steps": [
  {{"action": "fill", "selector": "#email", "value": "test@example.com", "description": "Enter email address"}},
  {{"action": "fill", "selector": "#password", "value": "TestPass123", "description": "Enter password"}},
  {{"action": "click", "selector": "button[type=submit]", "description": "Click submit button"}}
]}}"""


def plan_journey_with_ai(
    candidate: dict, email: str, password: str, api_key: str
) -> JourneyPlan | None:
    """Use Gemini to generate an execution plan for a detected journey."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    prompt = PLAN_PROMPT.format(
        journey_type=candidate["journey_type"],
        label=candidate["label"],
        url=candidate["page_url"],
        form_json=json.dumps(candidate["form"], indent=2, default=str),
        email=email or "testuser@example.com",
        password=password or "TestPassword123!",
    )

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        data = json.loads(text)
        steps = data.get("steps", [])
        if not steps:
            return None

        # Build form selector
        form = candidate["form"]
        if form.get("id"):
            form_selector = f"#{form['id']}"
        elif form.get("action"):
            form_selector = f"form[action='{form['action']}']"
        else:
            form_selector = f"form:nth-of-type({candidate['form_index'] + 1})"

        return JourneyPlan(
            journey_type=candidate["journey_type"],
            journey_name=candidate["label"],
            start_url=candidate["page_url"],
            form_selector=form_selector,
            steps=steps,
            requires_credentials=candidate["journey_type"] in ("login", "signup"),
        )
    except Exception:
        return None


# ── Playwright journey execution ─────────────────────────────────────


async def execute_journey(plan: JourneyPlan, on_progress=None) -> JourneyResult:
    """Execute a journey plan using Playwright, taking screenshots at each step."""
    steps_done = []
    start_time = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # Navigate to start URL
        try:
            await page.goto(plan.start_url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(plan.start_url, wait_until="load", timeout=30000)
        await page.wait_for_timeout(1500)

        # Take initial screenshot
        initial_screenshot = await page.screenshot(type="png")
        initial_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 5000)")
        steps_done.append(JourneyStep(
            step_number=0,
            action="navigate",
            description=f"Navigate to {plan.start_url}",
            selector="",
            value=plan.start_url,
            screenshot=initial_screenshot,
            visible_text=initial_text,
            url_before="",
            url_after=page.url,
        ))

        overall_success = True

        # Execute each step
        for i, step in enumerate(plan.steps):
            action = step.get("action", "")
            selector = step.get("selector", "")
            value = step.get("value", "")
            description = step.get("description", f"Step {i+1}")
            url_before = page.url
            step_errors = []

            if on_progress:
                on_progress(f"    Step {i+1}: {description}")

            try:
                if action == "fill":
                    # Try to find and fill the field
                    locator = page.locator(selector).first
                    await locator.wait_for(state="visible", timeout=5000)
                    await locator.click()
                    await locator.fill(value)
                    await page.wait_for_timeout(300)

                elif action == "click":
                    locator = page.locator(selector).first
                    await locator.wait_for(state="visible", timeout=5000)
                    await locator.click()
                    # Wait for potential navigation or DOM change
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        await page.wait_for_timeout(2000)

                elif action == "wait":
                    timeout_ms = int(step.get("timeout", 3000))
                    await page.wait_for_timeout(timeout_ms)

                # Take screenshot after action
                await page.wait_for_timeout(500)
                screenshot = await page.screenshot(type="png")
                visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 5000)")

                steps_done.append(JourneyStep(
                    step_number=i + 1,
                    action=action,
                    description=description,
                    selector=selector,
                    value=value if action != "fill" or "password" not in selector.lower() else "••••••••",
                    screenshot=screenshot,
                    visible_text=visible_text,
                    url_before=url_before,
                    url_after=page.url,
                    console_errors=list(console_errors),
                    success=True,
                ))
                console_errors.clear()

            except Exception as e:
                # Step failed — take screenshot of current state and stop
                try:
                    screenshot = await page.screenshot(type="png")
                    visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 5000)")
                except Exception:
                    screenshot = b""
                    visible_text = ""

                steps_done.append(JourneyStep(
                    step_number=i + 1,
                    action=action,
                    description=description,
                    selector=selector,
                    value=value if action != "fill" or "password" not in selector.lower() else "••••••••",
                    screenshot=screenshot,
                    visible_text=visible_text,
                    url_before=url_before,
                    url_after=page.url,
                    console_errors=list(console_errors),
                    success=False,
                    error_message=str(e)[:200],
                ))
                overall_success = False
                break

        await browser.close()

    duration = (time.time() - start_time) * 1000

    return JourneyResult(
        journey_type=plan.journey_type,
        journey_name=plan.journey_name,
        start_url=plan.start_url,
        steps=steps_done,
        overall_success=overall_success,
        duration_ms=duration,
    )


# ── Orchestrator ─────────────────────────────────────────────────────


async def run_journeys(
    site_data: SiteData,
    api_key: str,
    email: str = "",
    password: str = "",
    on_progress=None,
) -> list[JourneyResult]:
    """Detect, plan, and execute user journeys."""
    if on_progress:
        on_progress("Detecting user journeys...")

    candidates = detect_journeys(site_data)

    if not candidates:
        if on_progress:
            on_progress("  No interactive journeys detected")
        return []

    if on_progress:
        on_progress(f"  Found {len(candidates)} journey candidate(s)")

    results = []

    for c in candidates:
        jtype = c["journey_type"]

        # Skip credential-required journeys if no creds provided
        if jtype in ("login", "signup") and not email:
            if on_progress:
                on_progress(f"  Skipping {c['label']} (no credentials provided)")
            continue

        if on_progress:
            on_progress(f"  Planning: {c['label']} on {c['page_url']}")

        plan = await asyncio.to_thread(
            plan_journey_with_ai, c, email, password, api_key
        )
        if not plan:
            if on_progress:
                on_progress(f"  Could not generate plan for {c['label']}")
            continue

        if on_progress:
            on_progress(f"  Executing: {c['label']} ({len(plan.steps)} steps)")

        result = await execute_journey(plan, on_progress)
        results.append(result)

        status = "PASS" if result.overall_success else "FAIL"
        if on_progress:
            on_progress(f"  {c['label']}: {status} ({len(result.steps)} steps, {result.duration_ms:.0f}ms)")

    return results
