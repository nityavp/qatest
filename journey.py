"""
User journey testing — two modes:
1. Interactive Element Testing: Click every button/CTA, record what happens
2. AI-Guided Journey: Gemini looks at the page and walks through it like a real user
"""
from __future__ import annotations
import asyncio
import json
import time
import base64
import hashlib
import google.generativeai as genai
from playwright.async_api import async_playwright, Page
from models import (
    Finding, SiteData, PageData,
    JourneyPlan, JourneyStep, JourneyResult,
)


# ── 1. Interactive Element Testing ───────────────────────────────────
# Finds all buttons/CTAs on the page, clicks each, records what happens.


async def test_interactive_elements(site_data: SiteData, on_progress=None) -> list[JourneyResult]:
    """Click every button and CTA on each crawled page. Return results."""
    results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )

        for page_data in site_data.pages:
            if on_progress:
                on_progress(f"  Testing buttons on: {page_data.url}")

            result = await _test_page_buttons(context, page_data, on_progress)
            if result and result.steps:
                results.append(result)

        await browser.close()

    return results


async def _test_page_buttons(context, page_data: PageData, on_progress=None) -> JourneyResult | None:
    """Click each button/CTA on a single page and record outcomes."""
    steps = []
    start_time = time.time()

    # Collect clickable elements from the already-crawled data
    clickables = []
    for cta in page_data.cta_elements:
        text = cta.get("text", "").strip()
        if not text or len(text) > 60:
            continue
        clickables.append(cta)
    # Also check form buttons
    for form in page_data.forms:
        for btn in form.get("buttons", []):
            text = btn.get("text", "").strip()
            if text and len(text) < 60:
                clickables.append({"text": text, "tag": "button", "id": btn.get("id", ""), "className": btn.get("className", "")})

    if not clickables:
        return None

    # Deduplicate by text
    seen_text = set()
    unique = []
    for c in clickables:
        t = c.get("text", "").strip().lower()
        if t and t not in seen_text:
            seen_text.add(t)
            unique.append(c)

    # Limit to 15 elements per page
    unique = unique[:15]

    for i, elem in enumerate(unique):
        text = elem.get("text", "").strip()
        elem_id = elem.get("id", "")
        elem_class = elem.get("className", "")
        tag = elem.get("tag", "button")

        # Build selector strategy
        if elem_id:
            selector = f"#{elem_id}"
        else:
            # Use text-based selector
            selector = f"{tag}:has-text('{text}')"

        page = await context.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            await page.goto(page_data.url, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(1000)

            # Screenshot before click
            before_screenshot = await page.screenshot(type="png")
            url_before = page.url

            # Find and click the element
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.scroll_into_view_if_needed()
            await page.wait_for_timeout(300)

            # Click
            await locator.click()

            # Wait for reaction
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)

            await page.wait_for_timeout(1000)

            # Screenshot after click
            after_screenshot = await page.screenshot(type="png")
            url_after = page.url
            visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")

            # Determine what happened
            navigated = url_after != url_before
            had_errors = len(console_errors) > 0

            steps.append(JourneyStep(
                step_number=i + 1,
                action="click_button",
                description=f'Click "{text}"' + (f" → navigated to {url_after}" if navigated else " → same page"),
                selector=selector,
                value=text,
                screenshot=after_screenshot,
                visible_text=visible_text,
                url_before=url_before,
                url_after=url_after,
                console_errors=list(console_errors),
                success=not had_errors,
                error_message=console_errors[0][:200] if console_errors else "",
            ))

            if on_progress:
                status = "→ " + url_after.split("/")[-1] if navigated else "→ same page"
                err = " (JS error)" if had_errors else ""
                on_progress(f'    [{i+1}/{len(unique)}] Click "{text}" {status}{err}')

        except Exception as e:
            try:
                after_screenshot = await page.screenshot(type="png")
            except Exception:
                after_screenshot = b""

            steps.append(JourneyStep(
                step_number=i + 1,
                action="click_button",
                description=f'Click "{text}" — FAILED: {str(e)[:100]}',
                selector=selector,
                value=text,
                screenshot=after_screenshot,
                visible_text="",
                url_before=page_data.url,
                url_after=page.url,
                success=False,
                error_message=str(e)[:200],
            ))

        finally:
            await page.close()
            console_errors.clear()

    duration = (time.time() - start_time) * 1000

    return JourneyResult(
        journey_type="button_test",
        journey_name=f"Button/CTA Testing — {page_data.title or page_data.url}",
        start_url=page_data.url,
        steps=steps,
        overall_success=all(s.success for s in steps),
        duration_ms=duration,
    )


# ── 2. AI-Guided User Journey ───────────────────────────────────────
# Gemini looks at the page, decides what a user would do, and we execute it.

JOURNEY_PROMPT = """You are simulating a real user visiting this website for the first time. Look at the screenshot and decide what the user would do step by step.

Page URL: {url}
Page Title: {title}
{cred_note}

Based on the screenshot, generate 4-8 steps that a typical user would take. Each step should be a concrete action.

For each step, provide a CSS selector that can be used with Playwright to find the element.

Selector priority (use the most reliable one available):
1. #id — if the element has an id
2. [name="..."] — for form fields
3. button:has-text("...") or a:has-text("...") — for buttons/links with text
4. [placeholder="..."] — for input fields
5. CSS class selector as last resort

Action types:
- "click": Click a button, link, or interactive element
- "fill": Type text into an input field
- "select": Choose an option from a dropdown

For login/signup flows, use these credentials: email = "{email}", password = "{password}"
For other forms (contact, search, etc.), use realistic test data.

Respond with ONLY this JSON (no markdown):
{{"journey_name": "Name of this user journey",
  "steps": [
    {{"action": "click", "selector": "a:has-text('Get Started')", "description": "Click the Get Started CTA"}},
    {{"action": "fill", "selector": "#email", "value": "test@example.com", "description": "Enter email"}},
    {{"action": "click", "selector": "button:has-text('Submit')", "description": "Submit the form"}}
  ]
}}"""

NEXT_STEP_PROMPT = """You are a user navigating a website. You just completed this action: "{last_action}"

The page now looks like the screenshot above. URL: {url}

What would the user do next? Provide the single next action.

If the journey seems complete (e.g., landed on a dashboard, saw a success message, or there's nothing more to do), respond with:
{{"action": "done", "description": "Journey complete — reason"}}

Otherwise:
{{"action": "click|fill|select", "selector": "CSS selector", "value": "value if fill", "description": "What to do"}}

Respond with ONLY JSON (no markdown)."""


async def run_ai_guided_journey(
    site_data: SiteData,
    api_key: str,
    email: str = "",
    password: str = "",
    on_progress=None,
) -> list[JourneyResult]:
    """Use Gemini to plan and guide user journeys through the site."""
    if not api_key:
        return []

    results = []

    # Run AI journey on the homepage (primary entry point)
    homepage = site_data.pages[0] if site_data.pages else None
    if not homepage:
        return []

    if on_progress:
        on_progress(f"  AI planning user journey from: {homepage.url}")

    # Ask Gemini for the journey plan
    plan = await asyncio.to_thread(
        _plan_journey_with_vision, homepage, api_key, email, password
    )

    if not plan:
        if on_progress:
            on_progress("  Could not generate journey plan")
        return []

    if on_progress:
        on_progress(f"  Executing: {plan.get('journey_name', 'User Journey')} ({len(plan.get('steps', []))} steps)")

    # Execute the planned journey
    result = await _execute_ai_journey(plan, homepage.url, api_key, email, password, on_progress)
    if result:
        results.append(result)

    # Also try form-based journeys for pages with forms
    for page in site_data.pages:
        has_forms = any(
            f.get("fields") for f in page.forms
        )
        if has_forms and page.url != homepage.url:
            if on_progress:
                on_progress(f"  AI planning form journey: {page.url}")
            form_plan = await asyncio.to_thread(
                _plan_journey_with_vision, page, api_key, email, password
            )
            if form_plan and form_plan.get("steps"):
                result = await _execute_ai_journey(form_plan, page.url, api_key, email, password, on_progress)
                if result:
                    results.append(result)
            if len(results) >= 4:
                break

    return results


def _plan_journey_with_vision(page_data: PageData, api_key: str, email: str, password: str) -> dict | None:
    """Send page screenshot to Gemini and get a journey plan."""
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    desktop = page_data.screenshots.get("desktop", b"")
    if not desktop:
        return None

    cred_note = ""
    if email:
        cred_note = f"User has an account: email={email}"

    parts = [
        {"mime_type": "image/png", "data": desktop},
        JOURNEY_PROMPT.format(
            url=page_data.url,
            title=page_data.title,
            email=email or "testuser@example.com",
            password=password or "TestPassword123!",
            cred_note=cred_note,
        ),
    ]

    try:
        response = model.generate_content(parts)
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text)
    except Exception:
        return None


async def _execute_ai_journey(
    plan: dict, start_url: str, api_key: str, email: str, password: str, on_progress=None
) -> JourneyResult | None:
    """Execute a Gemini-planned journey using Playwright."""
    steps_done = []
    start_time = time.time()
    journey_name = plan.get("journey_name", "User Journey")
    planned_steps = plan.get("steps", [])

    if not planned_steps:
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        # Navigate to start
        try:
            await page.goto(start_url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(start_url, wait_until="load", timeout=30000)
        await page.wait_for_timeout(1500)

        # Initial screenshot
        init_screenshot = await page.screenshot(type="png")
        init_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
        steps_done.append(JourneyStep(
            step_number=0,
            action="navigate",
            description=f"Open {start_url}",
            selector="", value=start_url,
            screenshot=init_screenshot,
            visible_text=init_text,
            url_after=page.url,
        ))

        overall_success = True

        for i, step in enumerate(planned_steps):
            action = step.get("action", "click")
            selector = step.get("selector", "")
            value = step.get("value", "")
            description = step.get("description", f"Step {i+1}")
            url_before = page.url

            if action == "done":
                break

            if on_progress:
                on_progress(f"    Step {i+1}: {description}")

            try:
                if action == "fill" and selector:
                    loc = page.locator(selector).first
                    await loc.wait_for(state="visible", timeout=5000)
                    await loc.click()
                    await loc.fill(value)
                    await page.wait_for_timeout(300)

                elif action == "click" and selector:
                    loc = page.locator(selector).first
                    await loc.wait_for(state="visible", timeout=5000)
                    await loc.scroll_into_view_if_needed()
                    await page.wait_for_timeout(200)
                    await loc.click()
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        await page.wait_for_timeout(2000)

                elif action == "select" and selector:
                    loc = page.locator(selector).first
                    await loc.select_option(value=value)
                    await page.wait_for_timeout(500)

                await page.wait_for_timeout(800)

                screenshot = await page.screenshot(type="png")
                visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
                new_errors = list(console_errors)
                console_errors.clear()

                display_value = value
                if "password" in selector.lower():
                    display_value = "••••••••"

                steps_done.append(JourneyStep(
                    step_number=i + 1,
                    action=action,
                    description=description,
                    selector=selector,
                    value=display_value,
                    screenshot=screenshot,
                    visible_text=visible_text,
                    url_before=url_before,
                    url_after=page.url,
                    console_errors=new_errors,
                    success=True,
                ))

            except Exception as e:
                try:
                    screenshot = await page.screenshot(type="png")
                    visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
                except Exception:
                    screenshot = b""
                    visible_text = ""

                steps_done.append(JourneyStep(
                    step_number=i + 1,
                    action=action,
                    description=description,
                    selector=selector,
                    value=value,
                    screenshot=screenshot,
                    visible_text=visible_text,
                    url_before=url_before,
                    url_after=page.url,
                    success=False,
                    error_message=str(e)[:200],
                ))
                overall_success = False
                # Don't break — try remaining steps on current page state

        await browser.close()

    duration = (time.time() - start_time) * 1000

    return JourneyResult(
        journey_type="ai_guided",
        journey_name=journey_name,
        start_url=start_url,
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
    """Run all journey tests: button clicking + AI-guided flows."""
    all_results = []

    # Phase 1: Click every button/CTA and record what happens
    if on_progress:
        on_progress("Phase 1: Testing all buttons and CTAs...")
    button_results = await test_interactive_elements(site_data, on_progress)
    all_results.extend(button_results)

    if on_progress:
        total_buttons = sum(len(r.steps) for r in button_results)
        failed = sum(1 for r in button_results for s in r.steps if not s.success)
        on_progress(f"  Tested {total_buttons} buttons, {failed} had errors")

    # Phase 2: AI-guided user journeys
    if api_key:
        if on_progress:
            on_progress("Phase 2: AI-guided user journey simulation...")
        ai_results = await run_ai_guided_journey(
            site_data, api_key, email, password, on_progress
        )
        all_results.extend(ai_results)
    else:
        if on_progress:
            on_progress("  Skipping AI journeys (no API key)")

    if on_progress:
        on_progress(f"  Total: {len(all_results)} journey(s) completed")

    return all_results
