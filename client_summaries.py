"""
"Explain Like Homeowner" summary templates.

Plain-English starter blurbs the user can drop onto an invoice to explain
what the job actually means in language a non-contractor can understand.
Reduces confusion + increases trust. The user can edit the text on each
invoice — the templates here are starting points, not final copy.

Each template supports {{jurisdiction}} substitution.
"""
from __future__ import annotations

import re


_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


TEMPLATES: list[dict] = [
    {
        "key":   "fence_violation",
        "name":  "Fence Violation Resolution",
        "body":  (
            "This project involves resolving an existing fence violation by "
            "bringing the installation into compliance with {{jurisdiction}} "
            "code requirements and obtaining the required final permit "
            "approval. We handle the permit paperwork, coordinate any required "
            "engineering, and schedule the inspections needed to close the "
            "case officially."
        ),
    },
    {
        "key":   "after_the_fact",
        "name":  "After-The-Fact Permit",
        "body":  (
            "This project involves filing the proper permit for work that was "
            "already completed at your property, then coordinating the "
            "inspections the city requires to bring it into legal compliance. "
            "When we're done, the project will be recorded as fully permitted "
            "and approved in {{jurisdiction}} records."
        ),
    },
    {
        "key":   "permit_expediting",
        "name":  "Permit Expediting",
        "body":  (
            "This project handles the entire permit application process on "
            "your behalf — preparing the paperwork, submitting it to "
            "{{jurisdiction}}, responding to any city review comments, and "
            "coordinating inspections through final approval. You won't need "
            "to navigate the city's review system yourself."
        ),
    },
    {
        "key":   "code_compliance",
        "name":  "Code Violation Compliance",
        "body":  (
            "This project addresses an open code violation on your property "
            "by preparing and submitting the documentation needed to resolve "
            "it with {{jurisdiction}}. Closing this violation properly "
            "prevents additional fines, liens, and complications during a "
            "future sale or refinance of the property."
        ),
    },
    {
        "key":   "engineering",
        "name":  "Engineering Coordination",
        "body":  (
            "This project includes hiring and coordinating a licensed "
            "professional engineer to prepare the stamped drawings or "
            "certifications the city requires for your permit. The engineer "
            "ensures the work meets {{jurisdiction}} structural and code "
            "standards so the permit can be approved."
        ),
    },
    {
        "key":   "roofing",
        "name":  "Roofing Permit / Compliance",
        "body":  (
            "This project involves filing the proper permit for roofing work "
            "at your property and coordinating the required inspections so "
            "the installation is recognized by {{jurisdiction}} as code-"
            "compliant. Roofing permits help protect your insurance coverage "
            "and your property's value at sale."
        ),
    },
    {
        "key":   "inspection_coord",
        "name":  "Inspection Coordination",
        "body":  (
            "This project covers scheduling and coordinating the inspections "
            "{{jurisdiction}} requires to close out your permit. We follow up "
            "with the inspection department, accommodate any re-inspections, "
            "and continue until your case is officially marked as complete."
        ),
    },
]

TEMPLATE_BY_KEY = {t["key"]: t for t in TEMPLATES}


def render(text: str | None, variables: dict | None = None) -> str:
    """Substitute {{key}} variables; unknown keys left in place."""
    if not text:
        return ""
    vars_ = variables or {}
    def repl(m: re.Match) -> str:
        k = m.group(1)
        v = vars_.get(k)
        return str(v) if v not in (None, "") else m.group(0)
    return _VAR_RE.sub(repl, text)


def render_template(key: str, variables: dict | None = None) -> str | None:
    """Look up a template by key and return its rendered body, or None."""
    t = TEMPLATE_BY_KEY.get(key)
    if not t:
        return None
    return render(t["body"], variables)
