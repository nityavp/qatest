import asyncio
import time
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright
from datetime import datetime
from models import PageData, SiteData, DEVICES

AXE_CDN = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.8.2/axe.min.js"


async def crawl_site(url: str, max_pages: int = 15, on_progress=None) -> SiteData:
    """Crawl a website and collect testing data from each page."""
    parsed = urlparse(url)
    base_domain = parsed.netloc
    if not parsed.scheme:
        url = "https://" + url
        base_domain = urlparse(url).netloc

    visited = set()
    to_visit = [url]
    pages = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        # Create a context per device breakpoint
        contexts = {}
        for device_name, spec in DEVICES.items():
            ctx_args = {
                "viewport": {"width": spec["width"], "height": spec["height"]},
                "ignore_https_errors": True,
            }
            if spec.get("is_mobile"):
                ctx_args["is_mobile"] = True
                ctx_args["has_touch"] = True
            contexts[device_name] = await browser.new_context(**ctx_args)

        while to_visit and len(pages) < max_pages:
            current_url = to_visit.pop(0)
            current_url = current_url.split("#")[0]
            if current_url in visited:
                continue
            visited.add(current_url)

            if on_progress:
                on_progress(f"  Crawling ({len(pages)+1}/{max_pages}): {current_url}")

            try:
                page_data = await _collect_page_data(
                    contexts, current_url, base_domain
                )
                pages.append(page_data)

                for link in page_data.links:
                    link = link.split("#")[0]
                    if link not in visited and urlparse(link).netloc == base_domain:
                        to_visit.append(link)
            except Exception as e:
                if on_progress:
                    on_progress(f"  Error on {current_url}: {e}")

        await browser.close()

    if on_progress:
        on_progress(f"  Crawled {len(pages)} page(s)")

    return SiteData(
        base_url=url,
        pages=pages,
        crawled_at=datetime.now().isoformat(),
    )


async def _collect_page_data(contexts: dict, url: str, base_domain: str) -> PageData:
    """Collect screenshots from all devices, DOM data, headers, console errors, a11y."""

    console_errors = []
    network_errors = []
    screenshots = {}

    # ── Primary device (desktop) — collect all DOM data from here ──
    desktop_ctx = contexts["desktop"]
    desktop_page = await desktop_ctx.new_page()
    desktop_page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    desktop_page.on(
        "pageerror",
        lambda err: console_errors.append(str(err)),
    )

    start = time.time()
    try:
        response = await desktop_page.goto(url, wait_until="networkidle", timeout=30000)
    except Exception:
        response = await desktop_page.goto(url, wait_until="load", timeout=30000)
    load_time = (time.time() - start) * 1000

    status_code = response.status if response else 0
    headers = dict(response.headers) if response else {}

    await desktop_page.wait_for_timeout(1500)

    screenshots["desktop"] = await desktop_page.screenshot(type="png")
    html = await desktop_page.content()

    # Extract meta tags
    meta_tags = await desktop_page.evaluate(
        """() => {
            const m = {};
            document.querySelectorAll('meta').forEach(el => {
                const k = el.getAttribute('name') || el.getAttribute('property') || el.getAttribute('http-equiv');
                if (k) m[k] = el.getAttribute('content') || '';
            });
            m['title'] = document.title || '';
            const vp = document.querySelector('meta[name=viewport]');
            if (vp) m['viewport'] = vp.getAttribute('content') || '';
            return m;
        }"""
    )

    # Extract same-domain links
    links = await desktop_page.evaluate(
        """(baseDomain) => {
            const s = new Set();
            document.querySelectorAll('a[href]').forEach(a => {
                try {
                    const u = new URL(a.href, window.location.origin);
                    if (u.hostname === baseDomain && u.protocol.startsWith('http')) {
                        u.hash = '';
                        s.add(u.href);
                    }
                } catch(e) {}
            });
            return [...s];
        }""",
        base_domain,
    )

    # Extract forms (with buttons for journey detection)
    forms = await desktop_page.evaluate(
        """() => {
            return [...document.querySelectorAll('form')].slice(0, 20).map(f => ({
                action: f.action,
                method: f.method,
                id: f.id || '',
                className: f.className || '',
                fields: [...f.querySelectorAll('input,select,textarea')].slice(0, 30).map(i => ({
                    type: i.type || i.tagName.toLowerCase(),
                    name: i.name || '',
                    required: i.required,
                    placeholder: i.placeholder || '',
                    id: i.id || '',
                    className: i.className || '',
                })),
                buttons: [...f.querySelectorAll('button,input[type=submit],input[type=button]')].map(b => ({
                    text: (b.textContent || b.value || '').trim(),
                    type: b.type || 'button',
                    id: b.id || '',
                    className: b.className || '',
                }))
            }));
        }"""
    )

    # Extract images
    images = await desktop_page.evaluate(
        """() => {
            return [...document.querySelectorAll('img')].slice(0, 50).map(i => ({
                src: i.src || '',
                alt: i.alt || '',
                width: i.naturalWidth,
                height: i.naturalHeight,
            }));
        }"""
    )

    # Extract visible text for copywriting analysis
    visible_text = await desktop_page.evaluate(
        """() => (document.body.innerText || '').substring(0, 8000)"""
    )

    # Extract CTA elements (buttons + prominent links)
    cta_elements = await desktop_page.evaluate(
        """() => {
            const ctas = [];
            document.querySelectorAll('button, a.btn, [role=button], input[type=submit], a[class*=cta], a[class*=button]').forEach(el => {
                const text = (el.textContent || el.value || '').trim();
                if (text && text.length < 100) {
                    ctas.push({
                        text: text,
                        tag: el.tagName.toLowerCase(),
                        href: el.href || '',
                        id: el.id || '',
                        className: el.className || '',
                    });
                }
            });
            return ctas.slice(0, 30);
        }"""
    )

    # Run axe-core accessibility audit
    axe_violations = []
    try:
        axe_violations = await desktop_page.evaluate(
            """async (cdnUrl) => {
                const s = document.createElement('script');
                s.src = cdnUrl;
                document.head.appendChild(s);
                await new Promise((res, rej) => {
                    s.onload = res;
                    s.onerror = rej;
                    setTimeout(rej, 10000);
                });
                const r = await axe.run();
                return r.violations.map(v => ({
                    id: v.id,
                    impact: v.impact,
                    description: v.description,
                    help: v.help,
                    helpUrl: v.helpUrl,
                    nodes: v.nodes.length,
                    targets: v.nodes.slice(0, 5).map(n => n.target.join(', ')),
                }));
            }""",
            AXE_CDN,
        )
    except Exception:
        pass

    await desktop_page.close()

    # ── Remaining devices — screenshots only ──
    for device_name in DEVICES:
        if device_name == "desktop":
            continue
        ctx = contexts[device_name]
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(1000)
            screenshots[device_name] = await page.screenshot(type="png")
        except Exception:
            screenshots[device_name] = b""
        await page.close()

    return PageData(
        url=url,
        title=meta_tags.get("title", ""),
        status_code=status_code,
        headers=headers,
        html=html,
        screenshots=screenshots,
        console_errors=console_errors,
        network_errors=network_errors,
        axe_violations=axe_violations,
        meta_tags=meta_tags,
        links=links,
        forms=forms,
        images=images,
        load_time_ms=load_time,
        visible_text=visible_text,
        cta_elements=cta_elements,
    )
