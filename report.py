from __future__ import annotations
import base64
import json
import os
from datetime import datetime
from models import Finding, SiteData, JourneyResult, DEVICES

# ── Scoring ──────────────────────────────────────────────────────────

CATEGORY_WEIGHTS = {
    "functional": 0.18,
    "security": 0.15,
    "usability": 0.15,
    "visual": 0.12,
    "accessibility": 0.12,
    "content": 0.05,
    "copywriting": 0.10,
    "journey": 0.13,
}

SEVERITY_PENALTY = {
    "critical": 20,
    "high": 12,
    "medium": 5,
    "low": 2,
}


def calculate_scores(findings: list[Finding]) -> dict:
    category_scores = {}
    category_counts = {cat: {"critical": 0, "high": 0, "medium": 0, "low": 0} for cat in CATEGORY_WEIGHTS}

    for f in findings:
        cat = f.category if f.category in CATEGORY_WEIGHTS else "functional"
        sev = f.severity if f.severity in SEVERITY_PENALTY else "medium"
        category_counts[cat][sev] += 1

    for cat, weight in CATEGORY_WEIGHTS.items():
        counts = category_counts[cat]
        penalty = sum(counts[s] * SEVERITY_PENALTY[s] for s in SEVERITY_PENALTY)
        score = max(0, 100 - penalty)
        category_scores[cat] = {"score": score, "weight": weight, "counts": counts}

    overall = sum(
        category_scores[cat]["score"] * category_scores[cat]["weight"]
        for cat in category_scores
    )
    overall = round(overall)
    return {"overall": overall, "categories": category_scores}


def _score_color(score):
    if score >= 80: return "#22c55e"
    if score >= 60: return "#eab308"
    if score >= 40: return "#f97316"
    return "#ef4444"


def _severity_badge(sev):
    colors = {"critical": "#ef4444", "high": "#f97316", "medium": "#eab308", "low": "#3b82f6"}
    bg = {"critical": "#fef2f2", "high": "#fff7ed", "medium": "#fefce8", "low": "#eff6ff"}
    return f'<span style="display:inline-block;padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;text-transform:uppercase;background:{bg.get(sev,"#f1f5f9")};color:{colors.get(sev,"#64748b")}">{sev}</span>'


def _source_badge(source):
    if source == "ai":
        return '<span style="display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:600;background:#f0f9ff;color:#0369a1;margin-left:6px">AI</span>'
    if source == "journey":
        return '<span style="display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:600;background:#fdf4ff;color:#9333ea;margin-left:6px">JOURNEY</span>'
    return '<span style="display:inline-block;padding:2px 8px;border-radius:9999px;font-size:10px;font-weight:600;background:#f1f5f9;color:#64748b;margin-left:6px">AUTO</span>'


def _category_icon(cat):
    icons = {
        "functional": "&#9881;",
        "usability": "&#128100;",
        "visual": "&#127912;",
        "security": "&#128274;",
        "accessibility": "&#9855;",
        "content": "&#128221;",
        "copywriting": "&#9997;",
        "journey": "&#128694;",
    }
    return icons.get(cat, "&#10003;")


def _category_label(cat):
    return cat.replace("_", " ").title()


# ── HTML Report ──────────────────────────────────────────────────────

def generate_report(
    site_data: SiteData,
    findings: list[Finding],
    scores: dict,
    output_dir: str,
    baseline: dict | None = None,
    journey_results: list[JourneyResult] | None = None,
):
    os.makedirs(output_dir, exist_ok=True)
    screenshots_dir = os.path.join(output_dir, "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)

    # Save page screenshots
    page_screenshots = []
    for i, page in enumerate(site_data.pages):
        device_paths = {}
        for device_name in DEVICES:
            screenshot = page.screenshots.get(device_name, b"")
            if screenshot:
                path = f"screenshots/page_{i}_{device_name}.png"
                with open(os.path.join(output_dir, path), "wb") as f:
                    f.write(screenshot)
                device_paths[device_name] = path
        page_screenshots.append({"url": page.url, "title": page.title, "devices": device_paths})

    # Save journey screenshots
    journey_screenshot_paths = []
    for jr in (journey_results or []):
        jr_paths = []
        for step in jr.steps:
            if step.screenshot:
                path = f"screenshots/journey_{jr.journey_type}_step_{step.step_number}.png"
                with open(os.path.join(output_dir, path), "wb") as f:
                    f.write(step.screenshot)
                jr_paths.append(path)
            else:
                jr_paths.append("")
        journey_screenshot_paths.append(jr_paths)

    # Save baseline JSON
    baseline_data = {
        "generated_at": datetime.now().isoformat(),
        "base_url": site_data.base_url,
        "overall_score": scores["overall"],
        "category_scores": {k: v["score"] for k, v in scores["categories"].items()},
        "findings": [f.to_dict() for f in findings],
        "pages_crawled": len(site_data.pages),
        "journeys_tested": len(journey_results or []),
    }
    with open(os.path.join(output_dir, "baseline.json"), "w") as f:
        json.dump(baseline_data, f, indent=2)

    # Delta
    delta = None
    if baseline:
        old_score = baseline.get("overall_score", 0)
        old_ids = {f["id"] for f in baseline.get("findings", [])}
        new_ids = {f.id for f in findings}
        resolved = old_ids - new_ids
        still_open = old_ids & new_ids
        new_issues = new_ids - old_ids
        old_findings_map = {f["id"]: f for f in baseline.get("findings", [])}
        delta = {
            "old_score": old_score, "new_score": scores["overall"],
            "delta_score": scores["overall"] - old_score,
            "resolved_count": len(resolved), "still_open_count": len(still_open), "new_count": len(new_issues),
            "resolved": [old_findings_map[fid] for fid in resolved if fid in old_findings_map],
            "new_issues": [f for f in findings if f.id in new_issues],
        }

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    findings_sorted = sorted(findings, key=lambda f: severity_order.get(f.severity, 9))
    by_category = {}
    for f in findings_sorted:
        by_category.setdefault(f.category, []).append(f)

    html = _build_html(
        site_data, findings_sorted, by_category, scores,
        page_screenshots, delta, journey_results or [], journey_screenshot_paths,
    )

    report_path = os.path.join(output_dir, "report.html")
    with open(report_path, "w") as f:
        f.write(html)
    return report_path


# ── Journey HTML builder ─────────────────────────────────────────────

def _build_journey_html(journey_results, journey_screenshot_paths):
    if not journey_results:
        return ""

    html = '<h2 style="font-size:18px;margin:32px 0 16px">&#128694; User Journey Tests</h2>'

    for jr_idx, jr in enumerate(journey_results):
        status_color = "#22c55e" if jr.overall_success else "#ef4444"
        status_label = "PASSED" if jr.overall_success else "FAILED"
        paths = journey_screenshot_paths[jr_idx] if jr_idx < len(journey_screenshot_paths) else []

        html += f"""
        <div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:20px;border-left:4px solid {status_color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
                <div>
                    <h3 style="margin:0;font-size:16px">{jr.journey_name}</h3>
                    <p style="margin:0;font-size:12px;color:#94a3b8">{jr.start_url} &middot; {len(jr.steps)} steps &middot; {jr.duration_ms:.0f}ms</p>
                </div>
                <span style="display:inline-block;padding:4px 14px;border-radius:9999px;font-size:12px;font-weight:700;background:{status_color}20;color:{status_color}">{status_label}</span>
            </div>

            <div style="display:flex;gap:0;overflow-x:auto;padding-bottom:8px">"""

        for step_idx, step in enumerate(jr.steps):
            step_color = "#22c55e" if step.success else "#ef4444"
            screenshot_path = paths[step_idx] if step_idx < len(paths) else ""
            # Mask password values in display
            display_value = step.value if "password" not in step.selector.lower() else "********"
            action_label = step.description or f"{step.action}: {display_value}"

            connector = ""
            if step_idx < len(jr.steps) - 1:
                connector = '<div style="flex-shrink:0;display:flex;align-items:center;padding:0 4px;color:#cbd5e1;font-size:20px">&#8594;</div>'

            html += f"""
                <div style="flex:0 0 220px;min-width:220px">
                    <div style="text-align:center;margin-bottom:6px">
                        <span style="display:inline-block;width:24px;height:24px;border-radius:50%;background:{step_color};color:white;font-size:12px;line-height:24px;font-weight:700">{step.step_number}</span>
                    </div>
                    <div style="font-size:11px;color:#64748b;text-align:center;margin-bottom:6px;height:32px;overflow:hidden">{action_label[:60]}</div>"""

            if screenshot_path:
                html += f'<img src="{screenshot_path}" style="width:100%;border:1px solid #e2e8f0;border-radius:6px" loading="lazy">'

            if not step.success:
                html += f'<div style="font-size:10px;color:#ef4444;margin-top:4px;text-align:center">{step.error_message[:80]}</div>'

            html += f"</div>{connector}"

        html += """
            </div>
        </div>"""

    return html


# ── Main HTML builder ────────────────────────────────────────────────

def _build_html(site_data, findings_sorted, by_category, scores, page_screenshots, delta, journey_results, journey_screenshot_paths):
    overall = scores["overall"]
    sc = _score_color(overall)
    total = len(findings_sorted)
    crit_count = sum(1 for f in findings_sorted if f.severity == "critical")
    high_count = sum(1 for f in findings_sorted if f.severity == "high")
    med_count = sum(1 for f in findings_sorted if f.severity == "medium")
    low_count = sum(1 for f in findings_sorted if f.severity == "low")
    run_label = "Verification Report (Run 2)" if delta else "Baseline Report (Run 1)"
    now = datetime.now().strftime("%B %d, %Y at %I:%M %p")
    journeys_tested = len(journey_results)

    # Delta HTML
    delta_html = ""
    if delta:
        d = delta
        arrow = "&#9650;" if d["delta_score"] > 0 else "&#9660;" if d["delta_score"] < 0 else "&#8212;"
        delta_color = "#22c55e" if d["delta_score"] > 0 else "#ef4444" if d["delta_score"] < 0 else "#64748b"
        delta_html = f"""
        <div style="background:#f0fdf4;border:1px solid #bbf7d0;border-radius:12px;padding:24px;margin-bottom:24px">
            <h2 style="margin:0 0 16px;font-size:20px;color:#166534">Comparison with Previous Run</h2>
            <div style="display:flex;gap:32px;flex-wrap:wrap;align-items:center">
                <div style="text-align:center">
                    <div style="font-size:14px;color:#64748b">Previous</div>
                    <div style="font-size:36px;font-weight:700;color:#64748b">{d['old_score']}</div>
                </div>
                <div style="font-size:32px;color:{delta_color}">{arrow}</div>
                <div style="text-align:center">
                    <div style="font-size:14px;color:#64748b">Current</div>
                    <div style="font-size:36px;font-weight:700;color:{sc}">{d['new_score']}</div>
                </div>
                <div style="text-align:center;padding:12px 24px;border-radius:8px;background:white">
                    <div style="font-size:14px;color:#64748b">Change</div>
                    <div style="font-size:28px;font-weight:700;color:{delta_color}">{'+' if d['delta_score']>0 else ''}{d['delta_score']}</div>
                </div>
            </div>
            <div style="display:flex;gap:24px;margin-top:16px;flex-wrap:wrap">
                <div><span style="font-size:24px;font-weight:700;color:#22c55e">{d['resolved_count']}</span> <span style="color:#64748b">Resolved</span></div>
                <div><span style="font-size:24px;font-weight:700;color:#64748b">{d['still_open_count']}</span> <span style="color:#64748b">Still Open</span></div>
                <div><span style="font-size:24px;font-weight:700;color:#f97316">{d['new_count']}</span> <span style="color:#64748b">New / Regression</span></div>
            </div>
        </div>"""
        if d["resolved"]:
            delta_html += '<div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:24px">'
            delta_html += '<h3 style="margin:0 0 16px;color:#166534">Resolved Issues</h3>'
            for rf in d["resolved"]:
                delta_html += f'<div style="padding:8px 0;border-bottom:1px solid #f1f5f9;color:#64748b;text-decoration:line-through">{rf.get("title","")} <span style="font-size:12px">({rf.get("category","")})</span></div>'
            delta_html += "</div>"
        if d["new_issues"]:
            delta_html += '<div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:24px">'
            delta_html += '<h3 style="margin:0 0 16px;color:#c2410c">New / Regression Issues</h3>'
            for nf in d["new_issues"]:
                delta_html += f'<div style="padding:8px 0;border-bottom:1px solid #f1f5f9">{_severity_badge(nf.severity)} <strong>{nf.title}</strong> <span style="font-size:12px;color:#64748b">({nf.category})</span><br><span style="font-size:13px;color:#64748b">{nf.description[:150]}</span></div>'
            delta_html += "</div>"

    # Category score cards
    cat_cards = ""
    for cat in CATEGORY_WEIGHTS:
        cs = scores["categories"][cat]
        c = _score_color(cs["score"])
        counts = cs["counts"]
        count_str = f'{counts["critical"]}C {counts["high"]}H {counts["medium"]}M {counts["low"]}L'
        cat_cards += f"""
        <div style="background:white;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08)">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:13px;font-weight:600">{_category_icon(cat)} {_category_label(cat)}</span>
                <span style="font-size:22px;font-weight:700;color:{c}">{cs['score']}</span>
            </div>
            <div style="height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden">
                <div style="height:100%;width:{cs['score']}%;background:{c};border-radius:3px"></div>
            </div>
            <div style="font-size:11px;color:#94a3b8;margin-top:6px">{count_str}</div>
        </div>"""

    # Issues by category
    issues_html = ""
    for cat in CATEGORY_WEIGHTS:
        cat_findings = by_category.get(cat, [])
        if not cat_findings:
            issues_html += f"""
            <div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:16px">
                <h3 style="margin:0;font-size:16px">{_category_icon(cat)} {_category_label(cat)}</h3>
                <p style="color:#22c55e;margin:12px 0 0;font-size:14px">&#10003; No issues found</p>
            </div>"""
            continue
        items = ""
        for f in cat_findings:
            items += f"""
            <div style="border-bottom:1px solid #f1f5f9;padding:16px 0">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                    {_severity_badge(f.severity)} {_source_badge(f.source)}
                    <strong style="font-size:14px">{f.title}</strong>
                </div>
                <p style="margin:0 0 6px;font-size:13px;color:#475569">{f.description}</p>
                <div style="font-size:12px;color:#94a3b8">
                    <strong>Location:</strong> {f.location}<br>
                    <strong>Impact:</strong> {f.impact}<br>
                    <strong>Suggested Fix:</strong> {f.suggestion}
                </div>
            </div>"""
        issues_html += f"""
        <div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:16px">
            <h3 style="margin:0 0 4px;font-size:16px">{_category_icon(cat)} {_category_label(cat)} <span style="font-weight:400;font-size:13px;color:#94a3b8">({len(cat_findings)} issue{'s' if len(cat_findings)!=1 else ''})</span></h3>
            {items}
        </div>"""

    # Journey section
    journey_html = _build_journey_html(journey_results, journey_screenshot_paths)

    # Page screenshots gallery
    device_flex = {
        "desktop": "flex:4;min-width:400px", "laptop": "flex:3;min-width:320px",
        "tablet": "flex:2;min-width:200px", "mobile": "flex:1;min-width:120px;max-width:200px",
    }
    gallery = ""
    for ps in page_screenshots:
        device_images = ""
        for device_name in ["desktop", "laptop", "tablet", "mobile"]:
            path = ps["devices"].get(device_name, "")
            if path:
                label = DEVICES[device_name]["label"]
                flex = device_flex.get(device_name, "flex:1")
                device_images += f"""
                <div style="{flex}">
                    <p style="font-size:11px;color:#94a3b8;margin:0 0 4px;font-weight:600">{label}</p>
                    <img src="{path}" style="width:100%;border:1px solid #e2e8f0;border-radius:8px" loading="lazy">
                </div>"""
        gallery += f"""
        <div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:20px;margin-bottom:16px">
            <h4 style="margin:0 0 4px;font-size:14px">{ps['title'] or ps['url']}</h4>
            <p style="margin:0 0 12px;font-size:12px;color:#94a3b8">{ps['url']}</p>
            <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">{device_images}</div>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>QA Report — {site_data.base_url}</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; background:#f1f5f9; color:#1e293b; line-height:1.6; }}
a {{ color:#2563eb; text-decoration:none; }}
@media print {{ body {{ background:white; }} .no-print {{ display:none !important; }} }}
</style>
</head>
<body>

<div style="background:linear-gradient(135deg,#0f172a 0%,#1e3a5f 100%);color:white;padding:40px 48px">
    <div style="max-width:1100px;margin:0 auto;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:24px">
        <div>
            <div style="font-size:12px;text-transform:uppercase;letter-spacing:2px;opacity:0.7;margin-bottom:4px">{run_label}</div>
            <h1 style="font-size:28px;font-weight:700;margin-bottom:4px">QA Test Report</h1>
            <p style="font-size:14px;opacity:0.8">{site_data.base_url}</p>
            <p style="font-size:12px;opacity:0.6">{now} &middot; {len(site_data.pages)} page(s) &middot; {journeys_tested} journey(s) tested</p>
        </div>
        <div style="text-align:center">
            <div style="width:120px;height:120px;border-radius:50%;border:6px solid {sc};display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,0.1)">
                <span style="font-size:42px;font-weight:800;color:{sc}">{overall}</span>
            </div>
            <div style="font-size:12px;margin-top:6px;opacity:0.7">Product Readiness Score</div>
        </div>
    </div>
</div>

<div style="max-width:1100px;margin:0 auto;padding:32px 24px">
    {delta_html}

    <div style="background:white;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,.08);padding:24px;margin-bottom:24px">
        <h2 style="font-size:18px;margin-bottom:12px">Executive Summary</h2>
        <div style="display:flex;gap:24px;flex-wrap:wrap">
            <div style="flex:1;min-width:200px">
                <p style="font-size:14px;color:#475569">
                    We tested <strong>{len(site_data.pages)} page(s)</strong> and <strong>{journeys_tested} user journey(s)</strong>
                    on <strong>{site_data.base_url}</strong>, finding <strong>{total} issue(s)</strong> across {len(by_category)} categories.
                </p>
            </div>
            <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
                <div style="text-align:center;padding:8px 16px;border-radius:8px;background:#fef2f2">
                    <div style="font-size:22px;font-weight:700;color:#ef4444">{crit_count}</div>
                    <div style="font-size:11px;color:#ef4444">Critical</div>
                </div>
                <div style="text-align:center;padding:8px 16px;border-radius:8px;background:#fff7ed">
                    <div style="font-size:22px;font-weight:700;color:#f97316">{high_count}</div>
                    <div style="font-size:11px;color:#f97316">High</div>
                </div>
                <div style="text-align:center;padding:8px 16px;border-radius:8px;background:#fefce8">
                    <div style="font-size:22px;font-weight:700;color:#eab308">{med_count}</div>
                    <div style="font-size:11px;color:#eab308">Medium</div>
                </div>
                <div style="text-align:center;padding:8px 16px;border-radius:8px;background:#eff6ff">
                    <div style="font-size:22px;font-weight:700;color:#3b82f6">{low_count}</div>
                    <div style="font-size:11px;color:#3b82f6">Low</div>
                </div>
            </div>
        </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:32px">
        {cat_cards}
    </div>

    {journey_html}

    <h2 style="font-size:18px;margin-bottom:16px">Detailed Findings</h2>
    {issues_html}

    <h2 style="font-size:18px;margin:32px 0 16px">Page Screenshots</h2>
    {gallery}

    <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:20px;margin-top:32px;font-size:12px;color:#94a3b8">
        <strong>Disclaimer:</strong> This report provides automated quality-assurance testing including surface-level security checks.
        The security section checks for common misconfigurations (missing headers, HTTPS, etc.) and does <strong>not</strong> replace a
        formal penetration test or security audit. Visual and usability assessments are AI-assisted and may include subjective observations.
        User journey tests simulate real user interactions; credentials provided were used only during testing and are not stored.
    </div>

    <div style="text-align:center;margin-top:32px;font-size:12px;color:#cbd5e1">
        Generated by QATest v2 &middot; {now}
    </div>
</div>

</body>
</html>"""

    return html
