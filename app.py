#!/usr/bin/env python3
"""
QATest — Web UI for AI-powered QA testing.

Usage:
    python3 app.py
    Then open http://localhost:5000
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

from crawler import crawl_site
from analyzer import run_analysis
from report import calculate_scores, generate_report

app = Flask(__name__)

REPORTS_DIR = os.path.join(os.path.dirname(__file__), "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# In-memory state for running tests
progress_queues: dict[str, queue.Queue] = {}
test_results: dict[str, dict] = {}


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
    baseline_data = data.get("baseline_json")  # Optional JSON string

    task_id = uuid.uuid4().hex[:10]
    progress_queues[task_id] = queue.Queue()

    thread = threading.Thread(
        target=_run_test_thread,
        args=(task_id, url, max_pages, baseline_data),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    """Server-Sent Events endpoint for real-time progress."""

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


# ── Background test runner ───────────────────────────────────────────


def _run_test_thread(task_id, url, max_pages, baseline_json_str):
    q = progress_queues[task_id]

    def on_progress(msg):
        q.put({"type": "progress", "message": msg})

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Step 1: Crawl
        q.put({"type": "step", "step": 1, "message": "Crawling site..."})
        site_data = loop.run_until_complete(
            crawl_site(url, max_pages=max_pages, on_progress=on_progress)
        )

        if not site_data.pages:
            q.put({"type": "error", "message": "Could not crawl any pages. Check the URL."})
            return

        q.put({"type": "progress", "message": f"Crawled {len(site_data.pages)} page(s)"})

        # Step 2: Analyse
        q.put({"type": "step", "step": 2, "message": "Running analysis..."})
        api_key = os.environ.get("GEMINI_API_KEY", "")
        findings = loop.run_until_complete(
            run_analysis(site_data, api_key=api_key, on_progress=on_progress)
        )

        q.put({"type": "progress", "message": f"Found {len(findings)} issue(s)"})

        # Step 3: Score
        q.put({"type": "step", "step": 3, "message": "Calculating scores..."})
        scores = calculate_scores(findings)

        # Step 4: Report
        q.put({"type": "step", "step": 4, "message": "Generating report..."})

        baseline = None
        if baseline_json_str:
            try:
                baseline = json.loads(baseline_json_str)
            except Exception:
                pass

        output_dir = os.path.join(REPORTS_DIR, task_id)
        report_path = generate_report(site_data, findings, scores, output_dir, baseline)

        # Store result
        test_results[task_id] = {
            "score": scores["overall"],
            "categories": {k: v["score"] for k, v in scores["categories"].items()},
            "total_issues": len(findings),
            "pages_tested": len(site_data.pages),
        }

        q.put(
            {
                "type": "done",
                "report_url": f"/reports/{task_id}/report.html",
                "baseline_url": f"/reports/{task_id}/baseline.json",
                "score": scores["overall"],
                "categories": {k: v["score"] for k, v in scores["categories"].items()},
                "total_issues": len(findings),
                "pages_tested": len(site_data.pages),
            }
        )

    except Exception as e:
        q.put({"type": "error", "message": str(e)})
    finally:
        loop.close()


if __name__ == "__main__":
    print()
    print("  QATest — AI-Powered QA Testing")
    print("  Open http://localhost:8080")
    print()
    app.run(debug=False, host="0.0.0.0", port=8080, threaded=True)
