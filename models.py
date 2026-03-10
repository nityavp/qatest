from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


# Device breakpoints for multi-device testing
DEVICES = {
    "desktop": {"width": 1920, "height": 1080, "label": "Desktop (1920x1080)"},
    "laptop": {"width": 1366, "height": 768, "label": "Laptop (1366x768)"},
    "tablet": {"width": 768, "height": 1024, "label": "Tablet (768x1024)", "is_mobile": True},
    "mobile": {"width": 390, "height": 844, "label": "Mobile (iPhone 14)", "is_mobile": True},
}


@dataclass
class PageData:
    url: str
    title: str
    status_code: int
    headers: dict
    html: str
    screenshots: dict  # device_name -> bytes (PNG)
    console_errors: list
    network_errors: list
    axe_violations: list
    meta_tags: dict
    links: list
    forms: list
    images: list
    load_time_ms: float
    visible_text: str = ""         # Body inner text (first 8000 chars)
    cta_elements: list = field(default_factory=list)  # Buttons/links with text


@dataclass
class SiteData:
    base_url: str
    pages: list
    crawled_at: str


@dataclass
class Finding:
    id: str
    category: str  # functional, usability, visual, security, accessibility, content, copywriting, journey
    severity: str  # critical, high, medium, low
    title: str
    description: str
    location: str
    impact: str
    suggestion: str
    source: str = "automated"  # "automated", "ai", "journey"

    def to_dict(self):
        return {
            "id": self.id,
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "impact": self.impact,
            "suggestion": self.suggestion,
            "source": self.source,
        }


# ── Journey models ──────────────────────────────────────────────────


@dataclass
class JourneyStep:
    step_number: int
    action: str             # "fill_field", "click_button", "navigate", "wait"
    description: str        # Human-readable e.g. "Fill email field"
    selector: str           # CSS selector used
    value: str              # Value filled or button text clicked
    screenshot: bytes       # PNG screenshot after action
    visible_text: str       # All visible text at this step
    url_before: str = ""
    url_after: str = ""
    console_errors: list = field(default_factory=list)
    success: bool = True
    error_message: str = ""


@dataclass
class JourneyPlan:
    journey_type: str       # "login", "signup", "contact", "search", "newsletter", "checkout"
    journey_name: str       # Human-readable label
    start_url: str
    form_selector: str      # CSS selector of the form element
    steps: list             # List of dicts: {"action": "fill", "selector": "#email", "value": "{email}"}
    requires_credentials: bool = False


@dataclass
class JourneyResult:
    journey_type: str
    journey_name: str
    start_url: str
    steps: list             # List of JourneyStep
    overall_success: bool = True
    duration_ms: float = 0
    findings: list = field(default_factory=list)  # Findings specific to this journey
