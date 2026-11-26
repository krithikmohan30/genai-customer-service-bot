"""One triage call per message.

Six internship tasks all need classification, so they share a single result:

    sentiment  label + confidence, history-aware
    risk       level + categories, deliberately independent of sentiment
    language   rough guess, refined in step 6
    entities   order id, amount, product, contact
    intent     short label used by ticket creation

`TRIAGE_MODE=rules` gives a deterministic classifier for tests; `auto` uses the
model and falls back to rules when there is no key, an error, or a bad payload.
Rule hits are merged back in as a safety net, because a calm "someone changed my
password" must never be scored as low risk.
"""

from __future__ import annotations

import re

from . import config, llm, security

LABELS = ["positive", "neutral", "negative", "frustrated", "urgent", "sarcastic"]
RISK_ORDER = ["none", "low", "medium", "high"]

TONE = {
    "positive": "warm",
    "neutral": "professional",
    "negative": "empathetic_concise",
    "frustrated": "apologetic_action",
    "urgent": "reassuring_action",
    "sarcastic": "calm_professional",
}


# --------------------------------------------------------------------------- patterns

_SARCASM = [
    r"great,?\s+(?:another|so)",
    r"just\s+great",
    r"wonderful",
    r"brilliant,?\s+(?:another|now)",
    r"perfect,?\s+another",
    r"oh,?\s+fantastic",
    r"thanks?\s+a\s+lot\s+for\s+(?:nothing|the\s+help|nothing\s+at\s+all)",
    r"love\s+how",
    r"really\s+appreciate\s+(?:the\s+)?(?:delay|wait|help)",
    r"well\s+done.*(?:again|broken|nothing)",
    r"nice,?\s+another",
    r"awesome,?\s+another",
]

_URGENT = [
    r"\burgent\b",
    r"\basap\b",
    r"\bimmediately\b",
    r"right\s+now",
    r"\bemergency\b",
    r"can'?t\s+(?:access|log\s?in|login|sign\s?in)",
    r"locked\s+out",
    r"(?:system|site|app|website)\s+(?:is\s+)?down",
    r"\boutage\b",
]

_FRUSTRATED = [
    r"frustrat",
    r"ridiculous",
    r"\bpathetic\b",
    r"\bworst\b",
    r"\buseless\b",
    r"\bangry\b",
    r"still\s+(?:not|waiting|nothing)",
    r"again\s+and\s+again",
    r"(?:third|fourth|fifth)\s+time",
    r"no\s+(?:one|body)\s+(?:is\s+)?(?:responding|replying)",
    r"\bunacceptable\b",
]

_NEGATIVE = [
    r"not\s+working",
    r"\bfail(?:ed|ing|ure)?\b",
    r"\berror\b",
    r"\bdelay(?:ed|s|ing)?\b",
    r"\bwaiting\b",
    r"\bwrong\b",
    r"\bmissing\b",
    r"\brefund\b",
    r"\bcancel",
    r"no\s+response",
    r"\bcomplain",
    r"\bproblem\b",
    r"\bissue\b",
]

_POSITIVE = [
    r"\bthanks?\b",
    r"\bthank\s+you\b",
    r"\bgreat\b",
    r"\bgood\b",
    r"\bhelpful\b",
    r"\bresolved\b",
    r"\bappreciate\b",
    r"\bperfect\b",
]

# Hindi (Devanagari), Tamil script, and the two transliterated variants. Task 1
# asks for multilingual sentiment, so the rules path has to work without the
# model; the LLM path handles the long tail.
_NEGATIVE_HI = [
    r"नहीं\s*चल",
    r"काम\s*नहीं",
    r"खराब",
    r"दिक्कत",
    r"समस्या",
    r"गलत",
    r"देरी",
    r"नहीं\s*मिला",
]
_FRUSTRATED_HI = [r"नाराज़", r"परेशान", r"बहुत\s*बार", r"बार\s*बार"]
_URGENT_HI = [r"तुरंत", r"जल्दी", r"इमरजेंसी", r"अभी\s*तक"]
_POSITIVE_HI = [r"धन्यवाद", r"शुक्रिया", r"अच्छा", r"बढ़िया"]

_NEGATIVE_TA = [
    r"வேலை\s*செய்ய",
    r"பிரச்சனை",
    r"தவறு",
    r"தாமதம்",
    r"வரவில்லை",
]
_FRUSTRATED_TA = [r"கோபம்", r"எரிச்சல்", r"மீண்டும்\s*மீண்டும்"]
_URGENT_TA = [r"அவசரம்", r"உடனே", r"உடனடி"]
_POSITIVE_TA = [r"நன்றி", r"நல்ல"]

_NEGATIVE_HINGLISH = [
    r"\bkaam\s+nahi\b",
    r"\bnahi\s+chal",
    r"\bkharab\b",
    r"\bproblem\s+hai\b",
    r"\bgalat\b",
    r"\bderi\b",
]
_FRUSTRATED_HINGLISH = [
    r"\bpareshan\b",
    r"\bbahut\s+baar\b",
    r"\bbaarr?\s+baarr?\b",
    r"\bnaraz\b",
]
_URGENT_HINGLISH = [r"\bjaldi\b", r"\bturant\b", r"\babhi\b"]
_POSITIVE_HINGLISH = [r"\bdhanyawad\b", r"\bshukriya\b", r"\bachh?a\b", r"\bbadhiya\b"]

_NEGATIVE_TANGLISH = [
    r"\bvelai\s+illa\b",
    r"\bvelai\s+aagala\b",
    r"\bprach?ana\b",
    r"\bthappu\b",
    r"\bvarala\b",
]
_FRUSTRATED_TANGLISH = [
    r"\bromba\s+kastam\b",
    r"\berichal\b",
    r"\bthirumba\s+thirumba\b",
]
_URGENT_TANGLISH = [r"\bavasaram\b", r"\budane\b", r"\bseekiram\b"]
_POSITIVE_TANGLISH = [r"\bnandri\b", r"\bnalla\b", r"\bsuper\b"]

_HINGLISH_WORDS = re.compile(
    r"\b(?:nahi|nahin|hai|kya|mera|meri|kaam|paisa|jaldi|karo|bhejo|"
    r"pareshan|kharab|shukriya|dhanyawad|badhiya)\b",
    re.I,
)
_TANGLISH_WORDS = re.compile(
    r"\b(?:velai|prach?ana|romba|nandri|illai|illa|sollunga|avasaram|"
    r"seekiram|thappu|erichal|udane|aagala|varala)\b",
    re.I,
)

_RISK: dict[str, list[str]] = {
    "account_compromise": [
        r"(?:someone|somebody|anyone)\s+(?:has\s+)?(?:changed|reset|accessed|logged\s+in)",
        r"password\s+(?:was\s+)?(?:changed|reset)",
        r"(?:changed|reset)\s+(?:my|the)\s+password\s+and\s+(?:it\s+wasn'?t|it\s+was\s+not|not)\s+me",
        r"didn'?t\s+(?:change|request)\s+(?:my\s+)?password",
        r"unauthori[sz]ed\s+(?:access|login|transaction|change)",
        r"account\s+(?:has\s+been\s+)?(?:hacked|compromised|taken\s+over)",
        r"hacked",
        r"otp\s+(?:was\s+)?(?:shared|leaked|stolen)",
    ],
    "duplicate_payment": [
        r"(?:charged|paid|debited|billed)\s+.*(?:twice|two\s+times|double)",
        r"duplicate\s+(?:payment|charge|transaction|debit)",
        r"double\s+(?:charge|debit|payment)",
        r"money\s+(?:was\s+)?(?:deducted|debited)\s+twice",
    ],
    "legal_threat": [
        r"\blegal\b",
        r"\blawyer\b",
        r"consumer\s+court",
        r"\bsue\b",
        r"\bpolice\s+complaint\b",
        r"file\s+a\s+case",
    ],
    "fraud": [
        r"\bfraud",
        r"\bscam",
        r"fake\s+(?:call|message|email|invoice)",
    ],
}

_ORDER_ID = [
    re.compile(r"\b(?:ORD|ORDER|TKT|INV)[-_ ]?(\d{4,10})\b", re.I),
    re.compile(r"#\s?(\d{5,10})\b"),
]
_AMOUNT = re.compile(r"(?:₹|rs\.?|inr)\s?([\d,]+(?:\.\d{1,2})?)", re.I)
_ERROR_CODE = re.compile(r"\b([A-Z]{2,5}[-_]\d{2,6})\b")
_PRODUCT_HINTS = re.compile(
    r"\b(?:course|subscription|plan|invoice|order|app|dashboard|billing|emi)\b", re.I
)


# --------------------------------------------------------------------------- helpers


def _hits(patterns: list[str], text: str) -> list[str]:
    """Return the matched snippets, not the patterns — they end up in audit logs."""
    found: list[str] = []
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            snippet = match.group(0).strip().lower()
            if snippet and snippet not in found:
                found.append(snippet)
    return found


def _confidence(base: float, hits: int, cap: float = 0.96) -> float:
    return round(min(cap, base + 0.08 * max(0, hits - 1)), 2)


def guess_language(text: str) -> str:
    """Dominant language only. Kept for callers that need one value."""
    return detect_languages(text)["language"]


def detect_languages(text: str) -> dict:
    """Dominant language plus every language visibly present in the message.

    A Tamil sentence with English nouns is not "Tamil only": the customer
    switched mid-sentence, and a later turn may switch fully. Both are recorded
    so the session can follow the customer instead of pinning the first guess.
    """
    devanagari = bool(re.search(r"[\u0900-\u097F]", text))
    tamil = bool(re.search(r"[\u0B80-\u0BFF]", text))
    latin_words = re.findall(r"[A-Za-z]{2,}", text)
    hinglish = bool(_HINGLISH_WORDS.search(text))
    tanglish = bool(_TANGLISH_WORDS.search(text))

    languages: list[str] = []
    if devanagari:
        languages.append("hi")
    if tamil:
        languages.append("ta")
    if not devanagari and not tamil:
        if hinglish:
            languages.append("hinglish")
        if tanglish:
            languages.append("tanglish")
    if latin_words and languages and "en" not in languages:
        languages.append("en")
    if not languages:
        languages.append("en")

    dominant = languages[0]
    if "hinglish" in languages and "tanglish" in languages:
        dominant = "mixed"
    return {
        "language": dominant,
        "languages": languages,
        "mixed": len(languages) > 1,
    }


def extract_entities(text: str) -> dict:
    order_id = None
    for pattern in _ORDER_ID:
        match = pattern.search(text)
        if match:
            order_id = match.group(0).strip()
            break

    amount = _AMOUNT.search(text)
    error_code = _ERROR_CODE.search(text)
    code = error_code.group(1) if error_code else None
    # Order identifiers look like error codes; do not report the same token twice.
    if code and code.upper().split("-")[0].split("_")[0] in {
        "ORD",
        "ORDER",
        "INV",
        "TKT",
    }:
        code = None
    emails = re.findall(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b", text)
    phones = re.findall(r"(?<!\d)(?:\+91[\s-]?)?[6-9]\d{9}(?!\d)", text)
    product = _PRODUCT_HINTS.search(text)

    return {
        "order_id": order_id,
        "amount": amount.group(1).replace(",", "") if amount else None,
        "error_code": code,
        "product": product.group(0).lower() if product else None,
        "email": emails[0] if emails else None,
        "phone": phones[0] if phones else None,
    }


def _max_risk(a: str, b: str) -> str:
    return a if RISK_ORDER.index(a) >= RISK_ORDER.index(b) else b


def _rule_risk(text: str) -> dict:
    found = {name: _hits(p, text) for name, p in _RISK.items()}
    categories = [name for name, hits in found.items() if hits]
    if not categories:
        return {"level": "none", "categories": [], "confidence": 0.6}
    level = "high"
    if categories == ["fraud"]:
        level = "medium"
    return {
        "level": level,
        "categories": categories,
        "confidence": 0.7 if len(categories) == 1 else 0.85,
    }


def _rule_sentiment(text: str, history: list[str] | None) -> dict:
    history = history or []
    scores = {
        "sarcastic": _hits(_SARCASM, text),
        "urgent": _hits(_URGENT, text)
        + _hits(_URGENT_HI, text)
        + _hits(_URGENT_TA, text)
        + _hits(_URGENT_HINGLISH, text)
        + _hits(_URGENT_TANGLISH, text),
        "frustrated": _hits(_FRUSTRATED, text)
        + _hits(_FRUSTRATED_HI, text)
        + _hits(_FRUSTRATED_TA, text)
        + _hits(_FRUSTRATED_HINGLISH, text)
        + _hits(_FRUSTRATED_TANGLISH, text),
        "negative": _hits(_NEGATIVE, text)
        + _hits(_NEGATIVE_HI, text)
        + _hits(_NEGATIVE_TA, text)
        + _hits(_NEGATIVE_HINGLISH, text)
        + _hits(_NEGATIVE_TANGLISH, text),
        "positive": _hits(_POSITIVE, text)
        + _hits(_POSITIVE_HI, text)
        + _hits(_POSITIVE_TA, text)
        + _hits(_POSITIVE_HINGLISH, text)
        + _hits(_POSITIVE_TANGLISH, text),
    }
    # Textual shouting counts as frustration, but only when the message is long
    # enough that caps are a signal rather than an acronym.
    if len(text) > 15 and (text.count("!") >= 2 or re.search(r"\b[A-Z]{4,}\b", text)):
        scores["frustrated"].append("shouting")

    for label in ("sarcastic", "urgent", "frustrated", "negative", "positive"):
        if scores[label]:
            return {
                "label": label,
                "confidence": _confidence(0.62, len(scores[label])),
                "signals": scores[label][:3],
            }

    # Two or more prior negative turns make a mild message count as frustration.
    prior_negative = sum(
        1 for turn in history if _hits(_NEGATIVE, turn) or _hits(_FRUSTRATED, turn)
    )
    if prior_negative >= 2:
        return {"label": "negative", "confidence": 0.55, "signals": ["history"]}

    return {"label": "neutral", "confidence": 0.6, "signals": []}


def _rule_intent(text: str, risk: dict, sentiment: dict) -> str:
    if "duplicate_payment" in risk["categories"]:
        return "billing_dispute"
    if "account_compromise" in risk["categories"]:
        return "account_security"
    if "legal_threat" in risk["categories"]:
        return "legal_escalation"
    if re.search(r"\brefund\b", text, re.I):
        return "refund_request"
    if re.search(r"\b(?:invoice|billing|payment|emi|charge)\b", text, re.I):
        return "billing_question"
    if re.search(r"\b(?:login|password|otp|access)\b", text, re.I):
        return "access_issue"
    if sentiment["label"] in ("frustrated", "negative", "sarcastic"):
        return "complaint"
    if sentiment["label"] in ("urgent",):
        return "urgent_issue"
    return "general_question"


def _rule_classify(message: str, history: list[str] | None = None) -> dict:
    sentiment = _rule_sentiment(message, history)
    risk = _rule_risk(message)
    sentiment["tone"] = TONE[sentiment["label"]]
    detected = detect_languages(message)
    return {
        "sentiment": sentiment,
        "risk": risk,
        "language": detected["language"],
        "languages": detected["languages"],
        "mixed": detected["mixed"],
        "entities": extract_entities(message),
        "intent": _rule_intent(message, risk, sentiment),
        "source": "rules",
    }


# --------------------------------------------------------------------------- llm

PROMPT = """You are a triage classifier for an Indian e-learning company's support desk.
Classify the latest customer message. Use the conversation history for context.

Return ONLY a JSON object, no prose, no code fences:
{{
  "sentiment": {{"label": "positive|neutral|negative|frustrated|urgent|sarcastic", "confidence": 0.0}},
  "risk": {{"level": "none|low|medium|high", "categories": ["account_compromise|duplicate_payment|legal_threat|fraud"], "confidence": 0.0}},
  "language": "en|hi|ta|tanglish|hinglish|mixed",
  "intent": "short_snake_case_label",
  "entities": {{"order_id": null, "amount": null, "product": null, "error_code": null}}
}}

Rules:
- Sentiment and risk are independent. "No rush, but someone changed my password" is
  neutral sentiment and HIGH risk.
- Sarcasm ("Great, another delay") is label "sarcastic", never "positive".
- Account compromise, duplicate payment, fraud and legal threats are HIGH risk.
- Being locked out, forgetting a password or being unable to log in is NOT account
  compromise. Only use that category when there is evidence of an unauthorised
  change or access.
- Confidence is a number between 0 and 1.

CONVERSATION HISTORY:
{history}

LATEST MESSAGE:
{message}
"""


def _merge(llm_out: dict, rules: dict, message: str) -> dict:
    """LLM wins on nuance; rules win on safety.

    A risk category proposed by the model is only accepted when the message
    contains evidence for it. Without this, "I can't access my account" is
    labelled account compromise and an after-hours urgent goes to the wrong
    queue instead of on-call.
    """
    sentiment = dict(rules["sentiment"])
    incoming = llm_out.get("sentiment") or {}
    label = str(incoming.get("label", "")).lower()
    if label in LABELS:
        sentiment["label"] = label
        sentiment["tone"] = TONE[label]
        try:
            sentiment["confidence"] = round(float(incoming.get("confidence", 0.7)), 2)
        except (TypeError, ValueError):
            sentiment["confidence"] = 0.7

    # Never let the model talk itself out of detected sarcasm.
    if rules["sentiment"]["label"] == "sarcastic" and label != "sarcastic":
        sentiment = dict(rules["sentiment"])

    # An explicit complaint keyword must not be reclassified as positive or
    # neutral. The model has no veto over its own safety net.
    rules_label = rules["sentiment"]["label"]
    if rules_label in ("negative", "frustrated", "urgent") and label in (
        "positive",
        "neutral",
    ):
        sentiment = dict(rules["sentiment"])
        sentiment["confidence"] = max(float(sentiment["confidence"]), 0.6)

    risk = dict(rules["risk"])
    incoming_risk = llm_out.get("risk") or {}
    llm_level = str(incoming_risk.get("level", "")).lower()
    proposed = [
        str(category)
        for category in (incoming_risk.get("categories") or [])
        if isinstance(category, str)
    ]
    evidenced = [
        category
        for category in proposed
        if category in _RISK and _hits(_RISK[category], message)
    ]

    if llm_level in RISK_ORDER:
        risk["level"] = _max_risk(llm_level, rules["risk"]["level"])

    risk["categories"] = sorted(set(rules["risk"]["categories"]) | set(evidenced))
    if risk["level"] == "high" and not risk["categories"]:
        # High risk with nothing behind it is not actionable; keep it visible
        # but below the escalation line.
        risk["level"] = "medium"
        risk["confidence"] = min(float(risk["confidence"]), 0.6)

    entities = dict(rules["entities"])
    for key, value in (llm_out.get("entities") or {}).items():
        if value and not entities.get(key):
            entities[key] = value

    rules_language = rules["language"]
    language = str(llm_out.get("language") or rules_language)
    if language not in ("en", "hi", "ta", "hinglish", "tanglish", "mixed"):
        language = rules_language
    # Script and transliteration are stronger evidence than the model's guess:
    # a Tamil-script turn stays Tamil even if the model calls it "mixed".
    if rules_language != "en":
        language = rules_language
    languages = list(rules.get("languages") or [rules_language])
    if rules_language == "en" and language not in languages:
        languages.append(language)

    intent = str(llm_out.get("intent") or rules["intent"])
    # Rules already resolve specific intents from evidence. The model may only
    # fill in when the rules see nothing more specific than "general_question".
    if rules["intent"] != "general_question":
        intent = rules["intent"]

    return {
        "sentiment": sentiment,
        "risk": risk,
        "language": language,
        "languages": languages,
        "mixed": len(languages) > 1,
        "entities": entities,
        "intent": intent,
        "source": "llm+rules",
    }


# --------------------------------------------------------------------------- api


def classify(message: str, history: list[str] | None = None) -> dict:
    rules = _rule_classify(message, history)
    mode = str(config.get("triage.mode", "auto")).lower()

    if mode == "rules" or not llm.available():
        return rules

    prompt = PROMPT.format(
        history="\n".join(f"- {security.mask(t)}" for t in (history or [])[-10:])
        or "(none)",
        message=security.mask(message),
    )
    try:
        parsed = llm.generate_json(prompt)
    except Exception:
        parsed = {}

    if not parsed:
        rules["source"] = "rules_fallback"
        return rules
    return _merge(parsed, rules, message)


def decide_escalation(
    triage: dict, message: str, history: list[str] | None = None
) -> dict | None:
    """Step 1 rule: high risk always escalates, whatever the tone.

    Step 3 extends this with business hours, on-call queues, repeated-negative
    streaks and the 15-minute unresolved timer.
    """
    risk = triage["risk"]
    if risk["level"] != "high" or not risk["categories"]:
        return None
    reason = risk["categories"][0]
    queues = config.get("escalation.queues", {}) or {}
    summary = security.mask(message.strip())[:200]
    return {
        "reason": reason,
        "condition": "risk_level=high",
        "queue": queues.get("trust_safety", "trust_and_safety"),
        "summary": summary,
    }
