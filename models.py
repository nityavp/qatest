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


@dataclass
class SiteData:
    base_url: str
    pages: list
    crawled_at: str


@dataclass
class Finding:
    id: str
    category: str  # functional, usability, visual, security, accessibility, content
    severity: str  # critical, high, medium, low
    title: str
    description: str
    location: str
    impact: str
    suggestion: str
    source: str = "automated"  # "automated" or "ai"

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
