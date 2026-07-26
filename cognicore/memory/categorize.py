"""
Auto-categorization — zero LLM calls.

Detects memory category from text content using keyword patterns.
Falls back to 'general' if no pattern matches.
"""

import re
from typing import Tuple

# Category patterns: (category_name, keywords/patterns, weight)
# More specific patterns listed first for priority matching
_CATEGORY_PATTERNS = [
    ("personal_details", [
        r"\b(?:my name|i(?:'m| am)|age \d|years? old|\d+ (?:yo|y\.o))\b",
        r"\b(?:born in|live[sd]? in|from (?:mumbai|delhi|bangalore|india|usa|uk))\b",
        r"\b(?:family|brother|sister|mother|father|wife|husband|son|daughter)\b",
        r"\b(?:birthday|date of birth)\b",
    ]),
    ("professional_details", [
        r"\b(?:work(?:s|ing)? (?:at|for|as)|job|role|position|title)\b",
        r"\b(?:company|employer|startup|founded|co-?founder|ceo|cto)\b",
        r"\b(?:engineer|developer|designer|manager|intern|freelanc)\b",
        r"\b(?:career|profession|occupation|hired|salary|team)\b",
    ]),
    ("technology", [
        r"\b(?:python|javascript|typescript|rust|go|java|ruby|c\+\+|swift)\b",
        r"\b(?:react|vue|angular|next\.?js|django|flask|fastapi|express)\b",
        r"\b(?:docker|kubernetes|aws|gcp|azure|railway|vercel|heroku)\b",
        r"\b(?:sqlite|postgres|mongo|redis|mysql|database|db)\b",
        r"\b(?:api|sdk|framework|library|package|module|pip|npm)\b",
        r"\b(?:git|github|gitlab|deploy|ci|cd|devops)\b",
        r"\b(?:mcp|langchain|openai|claude|llm|gpt|ai|ml|model)\b",
        r"\b(?:cognicore|mem0|memory|embedding|vector|bm25|fts)\b",
    ]),
    ("milestones", [
        r"\b(?:launch|launched|released|shipped|published|submitted)\b",
        r"\b(?:achieved|milestone|reached|hit \d|crossed \d)\b",
        r"\b(?:downloads?|stars?|users?|installs?)\b.*\b\d+",
        r"\b\d+\s*(?:downloads?|stars?|users?|installs?)\b",
        r"\b(?:award|won|prize|recognition|featured)\b",
        r"\b(?:version|v\d|v\d\.\d)\b",
    ]),
    ("goals", [
        r"\b(?:want to|plan(?:ning)? to|goal|target|aim|intend)\b",
        r"\b(?:next step|roadmap|todo|to-?do|will (?:do|build|add))\b",
        r"\b(?:should|need to|must|priority|backlog|upcoming)\b",
        r"\b(?:future|eventually|soon|later|next (?:week|month|sprint))\b",
    ]),
    ("preferences", [
        r"\b(?:prefer|like|love|enjoy|favorite|favour)\b",
        r"\b(?:dislike|hate|avoid|don'?t (?:like|want|use))\b",
        r"\b(?:style|theme|dark mode|light mode|dracula|monokai)\b",
        r"\b(?:always use|never use|best practice|convention)\b",
        r"\b(?:choice|chosen|pick|go with|opt for)\b",
    ]),
    ("bugfix", [
        r"\b(?:bug|fix(?:ed)?|crash|error|exception|traceback)\b",
        r"\b(?:issue|problem|broken|failed|failing|regression)\b",
        r"\b(?:debug|patch|hotfix|workaround|resolved)\b",
    ]),
    ("architecture", [
        r"\b(?:architect|design|pattern|structure|layer|component)\b",
        r"\b(?:microservice|monolith|event.driven|pub.?sub)\b",
        r"\b(?:middleware|router|handler|controller|service)\b",
        r"\b(?:schema|migration|model|orm|entity)\b",
    ]),
]

# Precompile all patterns
_COMPILED_PATTERNS = []
for cat, patterns, *_ in _CATEGORY_PATTERNS:
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    _COMPILED_PATTERNS.append((cat, compiled))


def auto_categorize(text: str) -> Tuple[str, float]:
    """Detect category from text content using keyword patterns.

    Returns:
        Tuple of (category_name, confidence 0.0-1.0)
        Returns ('general', 0.0) if no pattern matches.
    """
    if not text or len(text.strip()) < 3:
        return ("general", 0.0)

    text_lower = text.lower()
    best_cat = "general"
    best_score = 0.0

    for cat, compiled_patterns in _COMPILED_PATTERNS:
        hits = 0
        for pattern in compiled_patterns:
            if pattern.search(text_lower):
                hits += 1

        if hits > 0:
            # Score: proportion of patterns matched, capped at 1.0
            score = min(hits / max(len(compiled_patterns) * 0.3, 1), 1.0)
            if score > best_score:
                best_score = score
                best_cat = cat

    return (best_cat, round(best_score, 2))


def categorize_facts(facts: list) -> list:
    """Categorize a list of atomic facts.

    Returns list of (fact_text, category, confidence) tuples.
    """
    results = []
    for fact in facts:
        cat, conf = auto_categorize(fact)
        results.append((fact, cat, conf))
    return results
