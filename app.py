#!/usr/bin/env python3
"""
QATest v2 — Web UI with test mode options and human-in-the-loop journey testing.
"""

import asyncio
import hashlib
import json
import os
import queue
import threading
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

load_dotenv()

from crawler import crawl_site
from analyzer import run_analysis
from journey import run_journeys
from copywriter import run_copy_analysis
from report import calculate_scores, generate_report
from models import Finding

app = Flask(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

progress_queues: dict = {}   # task_id -> Queue (server → browser)
resume_queues: dict = {}     # task_id -> Queue (browser → server, for human-in-the-loop)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start-test", methods=["POST"])
def start_test():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400
    if not url.startswith("http"):
        url = "https://" + url

    max_pages = int(data.get("max_pages", 10))
    baseline_data = data.get("baseline_json")
    email = data.get("email", "").strip()
    password = data.get("password", "").strip()
    mode = data.get("mode", "full")  # "full", "journey", "quick"

    task_id = uuid.uuid4().hex[:10]
    progress_queues[task_id] = queue.Queue()
    resume_queues[task_id] = queue.Queue()

    thread = threading.Thread(
        target=_run_test_thread,
        args=(task_id, url, max_pages, baseline_data, email, password, mode),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/resume/<task_id>", methods=["POST"])
def resume_test(task_id):
    """User clicked Continue after manually interacting with the browser."""
    q = resume_queues.get(task_id)
    if q:
        q.put("continue")
        return jsonify({"ok": True})
    return jsonify({"error": "Unknown task"}), 404


@app.route("/progress/<task_id>")
def progress(task_id):
    def generate():
        q = progress_queues.get(task_id)
        if not q:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Unknown task'})}\n\n"
            return
        while True:
            try:
                msg = q.get(timeout=120)
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
                continue
            yield f"data: {json.dumps(msg)}\n\n"
            if msg.get("type") in ("done", "error"):
                break

    return Response(generate(), mimetype="text/event-stream")


@app.route("/reports/<path:filename>")
def serve_report(filename):
    return send_from_directory(REPORTS_DIR, filename)


def _run_test_thread(task_id, url, max_pages, baseline_json_str, email, password, mode):
    q = progress_queues[task_id]
    rq = resume_queues[task_id]

    def on_progress(msg):
        q.put({"type": "progress", "message": msg})

    # Human-in-the-loop: pause journey and wait for user
    async def on_need_human(message):
        q.put({"type": "pause", "message": message})
        # Block until user clicks Continue
        rq.get(timeout=300)  # 5 min timeout

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        findings = []
        journey_results = []
        total_steps = {"full": 6, "journey": 3, "quick": 3}[mode]

        # ── STEP 1: Crawl (all modes) ─────────────────────────────
        q.put({"type": "step", "step": 1, "total_steps": total_steps, "message": "Crawling site..."})
        site_data = loop.run_until_complete(
            crawl_site(url, max_pages=max_pages, on_progress=on_progress)
        )
        if not site_data.pages:
            q.put({"type": "error", "message": "Could not crawl any pages. Check the URL."})
            return
        q.put({"type": "progress", "message": f"Crawled {len(site_data.pages)} page(s)"})

        if mode == "full":
            # ── STEP 2: Automated + AI analysis ───────────────────
            q.put({"type": "step", "step": 2, "total_steps": total_steps, "message": "Running analysis..."})
            findings = loop.run_until_complete(
                run_analysis(site_data, api_key=api_key, on_progress=on_progress)
            )
            q.put({"type": "progress", "message": f"Found {len(findings)} issue(s)"})

            # ── STEP 3: Journeys ──────────────────────────────────
            q.put({"type": "step", "step": 3, "total_steps": total_steps, "message": "Testing buttons & journeys..."})
            journey_results = loop.run_until_complete(
                run_journeys(site_data, api_key, email, password, on_progress, on_need_human)
            )
            _journeys_to_findings(journey_results, findings)

            # ── STEP 4: Copywriting ───────────────────────────────
            q.put({"type": "step", "step": 4, "total_steps": total_steps, "message": "Analyzing copywriting..."})
            if api_key:
                copy_findings = loop.run_until_complete(
                    run_copy_analysis(site_data, journey_results, api_key, on_progress)
                )
                findings.extend(copy_findings)

            # ── STEP 5: Score ─────────────────────────────────────
            q.put({"type": "step", "step": 5, "total_steps": total_steps, "message": "Calculating scores..."})
            scoring_findings = [f for f in findings if not f.id.startswith("journey-pass-")]
            scores = calculate_scores(scoring_findings)

            # ── STEP 6: Report ────────────────────────────────────
            q.put({"type": "step", "step": 6, "total_steps": total_steps, "message": "Generating report..."})

        elif mode == "journey":
            # ── STEP 2: Journeys only ─────────────────────────────
            q.put({"type": "step", "step": 2, "total_steps": total_steps, "message": "Testing buttons & journeys..."})
            journey_results = loop.run_until_complete(
                run_journeys(site_data, api_key, email, password, on_progress, on_need_human)
            )
            _journeys_to_findings(journey_results, findings)

            # ── STEP 3: Report ────────────────────────────────────
            q.put({"type": "step", "step": 3, "total_steps": total_steps, "message": "Generating report..."})
            scoring_findings = [f for f in findings if not f.id.startswith("journey-pass-")]
            scores = calculate_scores(scoring_findings)

        elif mode == "quick":
            # ── STEP 2: Automated checks only (no AI) ─────────────
            q.put({"type": "step", "step": 2, "total_steps": total_steps, "message": "Running automated checks..."})
            findings = loop.run_until_complete(
                run_analysis(site_data, api_key=None, on_progress=on_progress)
            )
            q.put({"type": "progress", "message": f"Found {len(findings)} issue(s)"})

            # ── STEP 3: Report ────────────────────────────────────
            q.put({"type": "step", "step": 3, "total_steps": total_steps, "message": "Generating report..."})
            scoring_findings = findings
            scores = calculate_scores(scoring_findings)

        # ── Generate report (all modes) ───────────────────────────
        baseline = None
        if baseline_json_str:
            try:
                baseline = json.loads(baseline_json_str)
            except Exception:
                pass

        output_dir = os.path.join(REPORTS_DIR, task_id)
        generate_report(site_data, findings, scores, output_dir, baseline, journey_results)

        q.put({
            "type": "done",
            "report_url": f"/reports/{task_id}/report.html",
            "baseline_url": f"/reports/{task_id}/baseline.json",
            "score": scores["overall"],
            "categories": {k: v["score"] for k, v in scores["categories"].items()},
            "total_issues": len(scoring_findings),
            "pages_tested": len(site_data.pages),
            "journeys_tested": len(journey_results),
        })

    except Exception as e:
        q.put({"type": "error", "message": str(e)})
    finally:
        loop.close()
        # Cleanup
        progress_queues.pop(task_id, None)
        resume_queues.pop(task_id, None)


def _journeys_to_findings(journey_results, findings):
    """Convert journey results into Finding objects."""
    for jr in journey_results:
        if jr.journey_type == "button_test":
            for step in jr.steps:
                if not step.success:
                    findings.append(Finding(
                        id=f"btn-err-{hashlib.md5((step.value + step.url_before).encode()).hexdigest()[:8]}",
                        category="functional",
                        severity="high",
                        title=f'Button "{step.value}" — Not Clickable',
                        description=f'Could not click "{step.value}": {step.error_message[:200]}',
                        location=step.url_before,
                        impact="This button/CTA does not work for users.",
                        suggestion="Ensure the element is visible, clickable, and has a working event handler.",
                        source="journey",
                    ))
                elif step.console_errors:
                    findings.append(Finding(
                        id=f"btn-jserr-{hashlib.md5((step.value + step.url_before).encode()).hexdigest()[:8]}",
                        category="functional",
                        severity="medium",
                        title=f'Button "{step.value}" — JS Error After Click',
                        description=f'Clicking triggered: {step.console_errors[0][:200]}',
                        location=step.url_before,
                        impact="JavaScript error may break functionality after this click.",
                        suggestion="Fix the JavaScript error in the click handler.",
                        source="journey",
                    ))
        else:
            if not jr.overall_success:
                failed = next((s for s in jr.steps if not s.success), None)
                findings.append(Finding(
                    id=f"journey-fail-{hashlib.md5(jr.journey_name.encode()).hexdigest()[:8]}",
                    category="journey", severity="critical",
                    title=f"{jr.journey_name} — Journey Failed",
                    description=f"Failed at step {failed.step_number}: {failed.error_message}" if failed else "Could not complete.",
                    location=jr.start_url,
                    impact="Users cannot complete this flow.",
                    suggestion="Fix the failing step and re-test.",
                    source="journey",
                ))


if __name__ == "__main__":
    print()
    print("  QATest v2 — AI-Powered QA Testing")
    print("  Open http://localhost:8080")
    print()
    app.run(debug=False, host="0.0.0.0", port=8080, threaded=True)
