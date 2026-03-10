#!/usr/bin/env python3
"""
QATest v2 — Web UI for AI-powered QA testing with user journey and copywriting analysis.

Usage:
    python3 app.py
    Then open http://localhost:8080
"""

import asyncio
import json
import os
import queue
import threading
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

load_dotenv()

import hashlib
from crawler import crawl_site
from analyzer import run_analysis
from journey import run_journeys
from copywriter import run_copy_analysis
from report import calculate_scores, generate_report
from models import Finding

app = Flask(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

progress_queues: dict = {}
test_results: dict = {}


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

    task_id = uuid.uuid4().hex[:10]
    progress_queues[task_id] = queue.Queue()

    thread = threading.Thread(
        target=_run_test_thread,
        args=(task_id, url, max_pages, baseline_data, email, password),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


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


def _run_test_thread(task_id, url, max_pages, baseline_json_str, email, password):
    q = progress_queues[task_id]

    def on_progress(msg):
        q.put({"type": "progress", "message": msg})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        api_key = os.environ.get("GEMINI_API_KEY", "")

        # Step 1: Crawl
        q.put({"type": "step", "step": 1, "total_steps": 6, "message": "Crawling site..."})
        site_data = loop.run_until_complete(
            crawl_site(url, max_pages=max_pages, on_progress=on_progress)
        )
        if not site_data.pages:
            q.put({"type": "error", "message": "Could not crawl any pages. Check the URL."})
            return
        q.put({"type": "progress", "message": f"Crawled {len(site_data.pages)} page(s)"})

        # Step 2: Automated + AI analysis
        q.put({"type": "step", "step": 2, "total_steps": 6, "message": "Running analysis..."})
        findings = loop.run_until_complete(
            run_analysis(site_data, api_key=api_key, on_progress=on_progress)
        )
        q.put({"type": "progress", "message": f"Found {len(findings)} issue(s) so far"})

        # Step 3: User journey tests (buttons + AI-guided)
        q.put({"type": "step", "step": 3, "total_steps": 6, "message": "Testing buttons & user journeys..."})
        journey_results = []
        journey_results = loop.run_until_complete(
            run_journeys(site_data, api_key, email, password, on_progress)
        )
        # Convert journey results to findings
        for jr in journey_results:
            if jr.journey_type == "button_test":
                # Individual button failures become findings
                for step in jr.steps:
                    if not step.success:
                        findings.append(Finding(
                            id=f"btn-err-{hashlib.md5((step.value + step.url_before).encode()).hexdigest()[:8]}",
                            category="functional",
                            severity="high" if "Error" in step.error_message else "medium",
                            title=f'Button "{step.value}" — Error on Click',
                            description=f'Clicking "{step.value}" caused: {step.error_message[:200]}',
                            location=step.url_before,
                            impact="Users clicking this button will encounter an error or broken behavior.",
                            suggestion="Check the click handler and ensure the element is functional.",
                            source="journey",
                        ))
                    elif step.console_errors:
                        findings.append(Finding(
                            id=f"btn-jserr-{hashlib.md5((step.value + step.url_before).encode()).hexdigest()[:8]}",
                            category="functional",
                            severity="medium",
                            title=f'Button "{step.value}" — JS Error After Click',
                            description=f'Clicking "{step.value}" triggered console error: {step.console_errors[0][:200]}',
                            location=step.url_before,
                            impact="JavaScript errors after clicking may break functionality.",
                            suggestion="Fix the JavaScript error triggered by this interaction.",
                            source="journey",
                        ))
            else:
                # AI-guided journey results
                if not jr.overall_success:
                    failed_step = next((s for s in jr.steps if not s.success), None)
                    findings.append(Finding(
                        id=f"journey-fail-{hashlib.md5(jr.journey_name.encode()).hexdigest()[:8]}",
                        category="journey",
                        severity="critical",
                        title=f"{jr.journey_name} — Journey Failed",
                        description=f"Failed at step {failed_step.step_number}: {failed_step.error_message}" if failed_step else "Could not complete.",
                        location=jr.start_url,
                        impact=f"Users cannot complete this flow.",
                        suggestion="Fix the failing step and re-test.",
                        source="journey",
                    ))
        else:
            q.put({"type": "progress", "message": "  Skipping journeys (no API key)"})

        # Step 4: Copywriting analysis
        q.put({"type": "step", "step": 4, "total_steps": 6, "message": "Analyzing copywriting..."})
        if api_key:
            copy_findings = loop.run_until_complete(
                run_copy_analysis(site_data, journey_results, api_key, on_progress)
            )
            findings.extend(copy_findings)
            q.put({"type": "progress", "message": f"Copywriting: found {len(copy_findings)} issue(s)"})
        else:
            q.put({"type": "progress", "message": "  Skipping copywriting (no API key)"})

        # Step 5: Score
        q.put({"type": "step", "step": 5, "total_steps": 6, "message": "Calculating scores..."})
        # Remove "passed journey" findings from scoring (they're informational)
        scoring_findings = [f for f in findings if not f.id.startswith("journey-pass-")]
        scores = calculate_scores(scoring_findings)

        # Step 6: Report
        q.put({"type": "step", "step": 6, "total_steps": 6, "message": "Generating report..."})

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


if __name__ == "__main__":
    print()
    print("  QATest v2 — AI-Powered QA Testing")
    print("  Open http://localhost:8080")
    print()
    app.run(debug=False, host="0.0.0.0", port=8080, threaded=True)
