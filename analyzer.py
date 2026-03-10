from __future__ import annotations
import asyncio
import base64
import hashlib
import json
import os
import httpx
from urllib.parse import urlparse
from models import Finding, PageData, SiteData, DEVICES

# ── Security header checks ──────────────────────────────────────────

REQUIRED_HEADERS = {
    "strict-transport-security": {
        "severity": "high",
        "title": "Missing HSTS Header",
        "description": "Strict-Transport-Security header is absent. Browsers will not enforce HTTPS, leaving users vulnerable to downgrade attacks.",
        "impact": "Users can be redirected to insecure HTTP, enabling man-in-the-middle attacks.",
        "suggestion": "Add the header: Strict-Transport-Security: max-age=31536000; includeSubDomains",
    },
    "content-security-policy": {
        "severity": "medium",
        "title": "Missing Content Security Policy",
        "description": "No Content-Security-Policy header found. The site has no protection against inline script injection.",
        "impact": "Increased risk of Cross-Site Scripting (XSS) attacks.",
        "suggestion": "Define a CSP that restricts script sources. Start with: Content-Security-Policy: default-src 'self'",
    },
    "x-content-type-options": {
        "severity": "medium",
        "title": "Missing X-Content-Type-Options Header",
        "description": "The X-Content-Type-Options header is not set to 'nosniff'. Browsers may MIME-sniff responses.",
        "impact": "Risk of MIME confusion attacks where browsers interpret files as a different content type.",
        "suggestion": "Add: X-Content-Type-Options: nosniff",
    },
    "x-frame-options": {
        "severity": "medium",
        "title": "Missing X-Frame-Options Header",
        "description": "No X-Frame-Options header. The site can be embedded in iframes on other domains.",
        "impact": "Vulnerable to clickjacking attacks where attackers overlay invisible iframes.",
        "suggestion": "Add: X-Frame-Options: DENY (or SAMEORIGIN if iframing is needed internally)",
    },
    "referrer-policy": {
        "severity": "low",
        "title": "Missing Referrer-Policy Header",
        "description": "No Referrer-Policy header set. Full URLs including query parameters may leak to third parties.",
        "impact": "Sensitive data in URLs could be exposed to external sites via the Referer header.",
        "suggestion": "Add: Referrer-Policy: strict-origin-when-cross-origin",
    },
    "permissions-policy": {
        "severity": "low",
        "title": "Missing Permissions-Policy Header",
        "description": "No Permissions-Policy (formerly Feature-Policy) header. Browser features like camera, microphone are not restricted.",
        "impact": "Third-party scripts could access sensitive browser APIs without restriction.",
        "suggestion": "Add: Permissions-Policy: camera=(), microphone=(), geolocation=()",
    },
}


def _hash_short(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()[:8]


def check_security(page_data: PageData) -> list[Finding]:
    findings = []
    headers_lower = {k.lower(): v for k, v in page_data.headers.items()}

    for header, info in REQUIRED_HEADERS.items():
        if header not in headers_lower:
            findings.append(
                Finding(
                    id=f"sec-{header}-{_hash_short(page_data.url)}",
                    category="security",
                    severity=info["severity"],
                    title=info["title"],
                    description=info["description"],
                    location=page_data.url,
                    impact=info["impact"],
                    suggestion=info["suggestion"],
                )
            )

    if page_data.url.startswith("http://"):
        findings.append(
            Finding(
                id=f"sec-https-{_hash_short(page_data.url)}",
                category="security",
                severity="critical",
                title="Site Not Using HTTPS",
                description="This page is served over plain HTTP. All data between the user and server is transmitted in clear text.",
                location=page_data.url,
                impact="All user data including passwords and personal info can be intercepted.",
                suggestion="Obtain an SSL certificate and redirect all HTTP traffic to HTTPS.",
            )
        )

    return findings


# ── Accessibility (from axe-core results) ───────────────────────────

IMPACT_TO_SEVERITY = {
    "critical": "critical",
    "serious": "high",
    "moderate": "medium",
    "minor": "low",
}


def check_accessibility(page_data: PageData) -> list[Finding]:
    findings = []

    for v in page_data.axe_violations:
        targets = v.get("targets", [])
        findings.append(
            Finding(
                id=f"a11y-{v['id']}-{_hash_short(page_data.url)}",
                category="accessibility",
                severity=IMPACT_TO_SEVERITY.get(v.get("impact", ""), "medium"),
                title=v.get("help", v["id"]),
                description=v.get("description", ""),
                location=f"{page_data.url} — {', '.join(targets[:3])}"
                if targets
                else page_data.url,
                impact=f"Affects {v.get('nodes', '?')} element(s). {v.get('impact', '').capitalize()} accessibility impact.",
                suggestion=f"See: {v.get('helpUrl', '')}",
            )
        )

    for img in page_data.images:
        if not img.get("alt") and img.get("src"):
            findings.append(
                Finding(
                    id=f"a11y-imgalt-{_hash_short(img['src'])}",
                    category="accessibility",
                    severity="medium",
                    title="Image Missing Alt Text",
                    description="The image has no alt attribute, making it invisible to screen readers.",
                    location=f"{page_data.url} — <img src=\"{img['src'][:80]}\">",
                    impact="Screen-reader users cannot understand the image content.",
                    suggestion='Add a descriptive alt attribute. Use alt="" only for purely decorative images.',
                )
            )

    return findings


# ── Meta / Content basics ────────────────────────────────────────────


def check_meta(page_data: PageData) -> list[Finding]:
    findings = []

    if not page_data.meta_tags.get("title"):
        findings.append(
            Finding(
                id=f"meta-title-{_hash_short(page_data.url)}",
                category="content",
                severity="high",
                title="Missing Page Title",
                description="The <title> tag is empty or missing.",
                location=page_data.url,
                impact="Poor SEO and confusing browser tab label for users.",
                suggestion="Add a unique, descriptive <title> for this page.",
            )
        )

    if not page_data.meta_tags.get("description"):
        findings.append(
            Finding(
                id=f"meta-desc-{_hash_short(page_data.url)}",
                category="content",
                severity="medium",
                title="Missing Meta Description",
                description="No <meta name='description'> tag found.",
                location=page_data.url,
                impact="Search engines will auto-generate a snippet which may not represent the page well.",
                suggestion="Add a 150-160 character meta description summarising this page's content.",
            )
        )

    if not page_data.meta_tags.get("viewport"):
        findings.append(
            Finding(
                id=f"meta-vp-{_hash_short(page_data.url)}",
                category="usability",
                severity="high",
                title="Missing Viewport Meta Tag",
                description="No <meta name='viewport'> tag. The page will not scale correctly on mobile devices.",
                location=page_data.url,
                impact="Mobile users will see a desktop-sized page, requiring pinch-zoom.",
                suggestion="Add: <meta name='viewport' content='width=device-width, initial-scale=1'>",
            )
        )

    for i, err in enumerate(page_data.console_errors[:10]):
        findings.append(
            Finding(
                id=f"console-{_hash_short(page_data.url)}-{i}",
                category="functional",
                severity="high"
                if "TypeError" in err or "ReferenceError" in err
                else "medium",
                title="JavaScript Console Error",
                description=f"Console error: {err[:300]}",
                location=page_data.url,
                impact="JavaScript errors can break interactive features and degrade user experience.",
                suggestion="Investigate and fix the JavaScript error. Check browser DevTools for a stack trace.",
            )
        )

    return findings


# ── Broken link checker ──────────────────────────────────────────────


async def check_broken_links(
    site_data: SiteData, on_progress=None
) -> list[Finding]:
    all_links = {}
    for page in site_data.pages:
        for link in page.links:
            if link not in all_links:
                all_links[link] = page.url
        for img in page.images:
            src = img.get("src", "")
            if src and src.startswith("http") and src not in all_links:
                all_links[src] = page.url

    crawled_urls = {p.url for p in site_data.pages}

    findings = []
    sem = asyncio.Semaphore(15)

    async def _check(url, source):
        if url in crawled_urls:
            return None
        async with sem:
            try:
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=12, verify=False
                ) as client:
                    r = await client.head(url)
                    if r.status_code >= 400:
                        return (url, r.status_code, source)
            except Exception:
                return (url, 0, source)
            return None

    tasks = [_check(url, src) for url, src in all_links.items()]
    results = await asyncio.gather(*tasks)

    for r in results:
        if r:
            url, code, source_page = r
            findings.append(
                Finding(
                    id=f"broken-{_hash_short(url)}",
                    category="functional",
                    severity="high" if code >= 500 or code == 0 else "medium",
                    title=f"Broken Link (HTTP {code})" if code else "Unreachable Link",
                    description=f"The URL {url} {'returned HTTP ' + str(code) if code else 'could not be reached'}.",
                    location=f"Found on: {source_page}",
                    impact="Users clicking this link will see an error page.",
                    suggestion="Update or remove the broken link.",
                )
            )

    if on_progress:
        on_progress(f"  Checked {len(all_links)} links, found {len(findings)} broken")

    return findings


# ── AI-powered analysis (Gemini Vision) ─────────────────────────────

AI_PROMPT = """You are a senior QA engineer reviewing a web page across multiple device viewports. You are provided screenshots from: Desktop (1920x1080), Laptop (1366x768), Tablet (768x1024), and Mobile (iPhone 14, 390x844).

Page URL: {url}
Page Title: {title}
HTTP Status: {status}
Console Errors: {console_errors}
Forms on page: {forms}
Number of images: {num_images}
Images without alt text: {images_no_alt}

Compare how the page renders across ALL device viewports and identify issues in these categories:

USABILITY — Navigation clarity, CTA visibility, form design, information hierarchy, mobile responsiveness, touch-target sizing, scroll/fold issues. Compare mobile vs desktop flows.

VISUAL — Layout alignment, spacing consistency, typography, colour contrast, responsive breakpoints, image quality, visual hierarchy. Flag layout breaks or elements that overflow/overlap at specific breakpoints.

CONTENT — Spelling/grammar errors, placeholder or lorem-ipsum text, broken copy, tone inconsistencies, missing microcopy. Check if text is truncated or unreadable on smaller screens.

FUNCTIONAL — Visible error states, broken UI elements, loading-spinner issues, console errors impact, missing interactive feedback. Note if interactive elements appear broken at certain viewport sizes.

Rules:
- Only flag genuine, actionable issues — not subjective preferences.
- Be specific about location AND which device/viewport the issue appears on.
- If an issue is present on multiple devices, list them.
- If the page looks solid in a category, return zero issues for it.

Respond with ONLY this JSON (no markdown fences):
{{"issues": [
  {{
    "category": "usability|visual|content|functional",
    "severity": "critical|high|medium|low",
    "title": "Short title",
    "description": "What is wrong",
    "location": "Where on the page and which device(s)",
    "impact": "Why it matters",
    "suggestion": "How to fix"
  }}
]}}"""


def analyze_with_ai_sync(page_data: PageData, api_key: str) -> list[Finding]:
    """Use Gemini Vision to analyze page screenshots. Runs synchronously."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")

    parts = []

    # Add all device screenshots
    for device_name in ["desktop", "laptop", "tablet", "mobile"]:
        screenshot = page_data.screenshots.get(device_name, b"")
        if screenshot:
            spec = DEVICES.get(device_name, {})
            parts.append({"mime_type": "image/png", "data": screenshot})
            parts.append(f"Above: {spec.get('label', device_name)} viewport")

    prompt_text = AI_PROMPT.format(
        url=page_data.url,
        title=page_data.title,
        status=page_data.status_code,
        console_errors=json.dumps(page_data.console_errors[:10]),
        forms=json.dumps(page_data.forms[:5], default=str),
        num_images=len(page_data.images),
        images_no_alt=sum(1 for i in page_data.images if not i.get("alt")),
    )
    parts.append(prompt_text)

    response = model.generate_content(parts)

    findings = []
    try:
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        data = json.loads(text)
        issues = data.get("issues", []) if isinstance(data, dict) else data

        for i, issue in enumerate(issues):
            findings.append(
                Finding(
                    id=f"ai-{_hash_short(page_data.url)}-{i}",
                    category=issue.get("category", "usability"),
                    severity=issue.get("severity", "medium"),
                    title=issue.get("title", "AI Finding"),
                    description=issue.get("description", ""),
                    location=f"{page_data.url} — {issue.get('location', '')}",
                    impact=issue.get("impact", ""),
                    suggestion=issue.get("suggestion", ""),
                    source="ai",
                )
            )
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    return findings


# ── Orchestrator ─────────────────────────────────────────────────────


async def run_analysis(
    site_data: SiteData, api_key: str | None = None, on_progress=None
) -> list[Finding]:
    all_findings: list[Finding] = []

    # 1. Automated checks
    if on_progress:
        on_progress("Running automated checks...")
    for page in site_data.pages:
        all_findings.extend(check_security(page))
        all_findings.extend(check_accessibility(page))
        all_findings.extend(check_meta(page))

    # 2. Broken links
    if on_progress:
        on_progress("Checking for broken links...")
    broken = await check_broken_links(site_data, on_progress)
    all_findings.extend(broken)

    # 3. AI analysis (Gemini)
    if api_key:
        for i, page in enumerate(site_data.pages):
            if on_progress:
                on_progress(
                    f"AI analysing page {i + 1}/{len(site_data.pages)}: {page.url}"
                )
            try:
                ai_findings = await asyncio.to_thread(
                    analyze_with_ai_sync, page, api_key
                )
                all_findings.extend(ai_findings)
            except Exception as e:
                if on_progress:
                    on_progress(f"  AI error for {page.url}: {e}")
    else:
        if on_progress:
            on_progress("  Skipping AI analysis (no GEMINI_API_KEY set)")

    # Deduplicate
    seen = set()
    unique = []
    for f in all_findings:
        key = (f.category, f.title, f.location)
        if key not in seen:
            seen.add(key)
            unique.append(f)

    return unique
