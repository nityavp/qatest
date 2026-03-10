"""
User journey testing:
1. Button/CTA click testing (headless, automated)
2. AI-guided user journey (VISIBLE browser, human-in-the-loop)
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

# Console errors to IGNORE (resource loading, not real JS errors)
NOISE_PATTERNS = [
    "failed to load resource",
    "net::err_",
    "favicon",
    "404 (not found)",
    "the server responded with a status of",
    "mixed content",
    "third-party cookie",
    "blocked by client",
    "downloadable font",
    "preload",
]


def _is_real_error(msg: str) -> bool:
    """Filter out resource-loading noise from real JS errors."""
    lower = msg.lower()
    return not any(p in lower for p in NOISE_PATTERNS)


# ── 1. Button/CTA Click Testing (headless) ──────────────────────────


async def test_interactive_elements(site_data: SiteData, on_progress=None) -> list[JourneyResult]:
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
    steps = []
    start_time = time.time()

    clickables = []
    for cta in page_data.cta_elements:
        text = cta.get("text", "").strip()
        if text and len(text) < 60:
            clickables.append(cta)
    for form in page_data.forms:
        for btn in form.get("buttons", []):
            text = btn.get("text", "").strip()
            if text and len(text) < 60:
                clickables.append({"text": text, "tag": "button", "id": btn.get("id", ""), "className": btn.get("className", "")})

    # Deduplicate by text
    seen = set()
    unique = []
    for c in clickables:
        t = c.get("text", "").strip().lower()
        if t and t not in seen:
            seen.add(t)
            unique.append(c)
    unique = unique[:12]

    if not unique:
        return None

    for i, elem in enumerate(unique):
        text = elem.get("text", "").strip()
        elem_id = elem.get("id", "")
        tag = elem.get("tag", "button")
        selector = f"#{elem_id}" if elem_id else f"{tag}:has-text('{text}')"

        page = await context.new_page()
        all_errors = []
        page.on("console", lambda msg: all_errors.append(msg.text) if msg.type == "error" else None)

        try:
            await page.goto(page_data.url, wait_until="networkidle", timeout=20000)
            await page.wait_for_timeout(800)

            # Clear pre-existing errors (from page load, not our click)
            all_errors.clear()

            url_before = page.url
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=5000)
            await locator.scroll_into_view_if_needed()
            await page.wait_for_timeout(200)
            await locator.click()

            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                await page.wait_for_timeout(2000)
            await page.wait_for_timeout(800)

            screenshot = await page.screenshot(type="png")
            url_after = page.url
            visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")

            # Only count REAL JS errors, not resource-loading noise
            real_errors = [e for e in all_errors if _is_real_error(e)]
            navigated = url_after != url_before

            steps.append(JourneyStep(
                step_number=i + 1,
                action="click_button",
                description=f'Click "{text}"' + (f" → {url_after}" if navigated else " → same page"),
                selector=selector,
                value=text,
                screenshot=screenshot,
                visible_text=visible_text,
                url_before=url_before,
                url_after=url_after,
                console_errors=real_errors,
                success=True,  # Click worked, navigation happened
                error_message=real_errors[0][:200] if real_errors else "",
            ))

            if on_progress:
                dest = url_after.split("/")[-1] if navigated else "same page"
                err = f" ({len(real_errors)} JS error)" if real_errors else ""
                on_progress(f'    [{i+1}/{len(unique)}] "{text}" → {dest}{err}')

        except Exception as e:
            try:
                screenshot = await page.screenshot(type="png")
            except Exception:
                screenshot = b""

            steps.append(JourneyStep(
                step_number=i + 1,
                action="click_button",
                description=f'Click "{text}" — element not found or not clickable',
                selector=selector, value=text, screenshot=screenshot,
                visible_text="", url_before=page_data.url, url_after=page.url,
                success=False,
                error_message=str(e)[:200],
            ))
        finally:
            await page.close()
            all_errors.clear()

    duration = (time.time() - start_time) * 1000
    return JourneyResult(
        journey_type="button_test",
        journey_name=f"Button/CTA Testing — {page_data.title or page_data.url}",
        start_url=page_data.url,
        steps=steps,
        overall_success=all(s.success for s in steps),
        duration_ms=duration,
    )


# ── 2. AI-Guided Journey (VISIBLE browser + human-in-the-loop) ──────

JOURNEY_PROMPT = """You are simulating a real user visiting this website. Look at the screenshot and create a step-by-step journey a typical user would take.

Page URL: {url}
Page Title: {title}
{cred_note}

Generate 4-8 steps. Each step is a concrete user action.

Selector priority:
1. #id
2. [name="..."]
3. button:has-text("...") or a:has-text("...")
4. [placeholder="..."]
5. CSS class selector

Action types: "click", "fill", "select"

For login/signup: email = "{email}", password = "{password}"
For other forms: use realistic test data.

Respond with ONLY JSON (no markdown):
{{"journey_name": "Name of journey",
  "steps": [
    {{"action": "click", "selector": "a:has-text('Get Started')", "description": "Click Get Started CTA"}},
    {{"action": "fill", "selector": "#email", "value": "test@example.com", "description": "Enter email"}}
  ]
}}"""


def _plan_journey_with_vision(page_data: PageData, api_key: str, email: str, password: str) -> dict | None:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    desktop = page_data.screenshots.get("desktop", b"")
    if not desktop:
        return None

    cred_note = f"User credentials: email={email}" if email else ""
    parts = [
        {"mime_type": "image/png", "data": desktop},
        JOURNEY_PROMPT.format(
            url=page_data.url, title=page_data.title,
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


async def execute_ai_journey_interactive(
    plan: dict,
    start_url: str,
    api_key: str,
    on_progress=None,
    on_need_human=None,  # Callback: async fn that pauses until user clicks Continue
) -> JourneyResult | None:
    """
    Execute AI journey with a VISIBLE browser.
    When a step fails, calls on_need_human() which pauses until the user
    interacts with the browser and clicks Continue in the UI.
    """
    steps_done = []
    start_time = time.time()
    journey_name = plan.get("journey_name", "User Journey")
    planned_steps = plan.get("steps", [])

    if not planned_steps:
        return None

    async with async_playwright() as p:
        # VISIBLE browser — user can see and interact
        browser = await p.chromium.launch(
            headless=False,
            args=["--window-size=1280,900", "--window-position=100,50"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        console_errors = []
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

        try:
            await page.goto(start_url, wait_until="networkidle", timeout=30000)
        except Exception:
            await page.goto(start_url, wait_until="load", timeout=30000)
        await page.wait_for_timeout(1500)

        init_screenshot = await page.screenshot(type="png")
        init_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
        steps_done.append(JourneyStep(
            step_number=0, action="navigate",
            description=f"Open {start_url}", selector="", value=start_url,
            screenshot=init_screenshot, visible_text=init_text, url_after=page.url,
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
                    await page.locator(selector).first.select_option(value=value)
                    await page.wait_for_timeout(500)

                await page.wait_for_timeout(800)

                screenshot = await page.screenshot(type="png")
                visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
                real_errors = [e for e in console_errors if _is_real_error(e)]
                console_errors.clear()

                display_value = "••••••••" if "password" in selector.lower() else value

                steps_done.append(JourneyStep(
                    step_number=i + 1, action=action, description=description,
                    selector=selector, value=display_value,
                    screenshot=screenshot, visible_text=visible_text,
                    url_before=url_before, url_after=page.url,
                    console_errors=real_errors, success=True,
                ))

            except Exception as e:
                # Step failed — ask human for help if callback available
                if on_need_human:
                    if on_progress:
                        on_progress(f"    ⚠ Step failed: {description}. Waiting for your help...")
                        on_progress(f"    ⚠ Please interact with the browser, then click Continue in the UI.")

                    # Wait for user to interact and click Continue
                    await on_need_human(
                        f'Step {i+1} failed: "{description}" — Element not found or not clickable. '
                        f'Please do this step manually in the browser, then click Continue.'
                    )

                    if on_progress:
                        on_progress(f"    ✓ User completed manual step, continuing...")

                    # User has interacted — take screenshot of current state
                    await page.wait_for_timeout(1000)
                    try:
                        screenshot = await page.screenshot(type="png")
                        visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
                    except Exception:
                        screenshot = b""
                        visible_text = ""

                    steps_done.append(JourneyStep(
                        step_number=i + 1, action="human_assist",
                        description=f"{description} (completed manually by user)",
                        selector=selector, value=value,
                        screenshot=screenshot, visible_text=visible_text,
                        url_before=url_before, url_after=page.url,
                        success=True,
                    ))
                else:
                    # No human callback — record failure and continue
                    try:
                        screenshot = await page.screenshot(type="png")
                        visible_text = await page.evaluate("() => (document.body.innerText || '').substring(0, 3000)")
                    except Exception:
                        screenshot = b""
                        visible_text = ""

                    steps_done.append(JourneyStep(
                        step_number=i + 1, action=action, description=description,
                        selector=selector, value=value,
                        screenshot=screenshot, visible_text=visible_text,
                        url_before=url_before, url_after=page.url,
                        success=False, error_message=str(e)[:200],
                    ))
                    overall_success = False

        # Keep browser open briefly so user can see final state
        await page.wait_for_timeout(2000)
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
    on_need_human=None,
) -> list[JourneyResult]:
    all_results = []

    # Phase 1: Button/CTA click testing (headless, automated)
    if on_progress:
        on_progress("Phase 1: Testing all buttons and CTAs...")
    button_results = await test_interactive_elements(site_data, on_progress)
    all_results.extend(button_results)

    if on_progress:
        total_buttons = sum(len(r.steps) for r in button_results)
        failed = sum(1 for r in button_results for s in r.steps if not s.success)
        on_progress(f"  Tested {total_buttons} buttons, {failed} could not be clicked")

    # Phase 2: AI-guided journey (VISIBLE browser)
    if api_key:
        if on_progress:
            on_progress("Phase 2: AI-guided user journey (browser will open)...")

        homepage = site_data.pages[0] if site_data.pages else None
        if homepage:
            if on_progress:
                on_progress(f"  Planning journey from: {homepage.url}")

            plan = await asyncio.to_thread(
                _plan_journey_with_vision, homepage, api_key, email, password
            )

            if plan and plan.get("steps"):
                if on_progress:
                    on_progress(f"  Executing: {plan.get('journey_name', 'Journey')} ({len(plan['steps'])} steps)")

                result = await execute_ai_journey_interactive(
                    plan, homepage.url, api_key, on_progress, on_need_human
                )
                if result:
                    all_results.append(result)

            # Test pages with forms too
            for page_data in site_data.pages[1:]:
                if any(f.get("fields") for f in page_data.forms):
                    if on_progress:
                        on_progress(f"  Planning form journey: {page_data.url}")
                    form_plan = await asyncio.to_thread(
                        _plan_journey_with_vision, page_data, api_key, email, password
                    )
                    if form_plan and form_plan.get("steps"):
                        result = await execute_ai_journey_interactive(
                            form_plan, page_data.url, api_key, on_progress, on_need_human
                        )
                        if result:
                            all_results.append(result)
                    if len(all_results) >= 5:
                        break
    else:
        if on_progress:
            on_progress("  Skipping AI journeys (no API key)")

    if on_progress:
        on_progress(f"  Total: {len(all_results)} journey(s) completed")

    return all_results
