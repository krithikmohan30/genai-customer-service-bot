"""Tone-adjusted replies.

The business policy text is never rewritten. Tone is applied as a short
acknowledgement in front of the answer and, when an escalation was raised, a
single line naming the queue. That keeps Task 1's "adjust the tone without
changing business policies" literally true: strip the prefix and the answer is
byte-identical to what the policy layer produced.
"""

from __future__ import annotations

LANGUAGES = ("en", "hi", "ta", "hinglish", "tanglish")

ACK: dict[str, dict[str, str]] = {
    "en": {
        "positive": "Glad that worked.",
        "neutral": "Thanks for the details. Let me check this for you.",
        "negative": "I'm sorry about the trouble. I'm looking at your issue now.",
        "frustrated": "I understand the frustration, and I'm sorry. I'm taking this up now.",
        "urgent": "I've flagged this as urgent and a specialist is picking it up now.",
        "sarcastic": "I hear you, and I'm sorry this keeps happening. I'm on it now.",
    },
    "hi": {
        "positive": "खुशी है कि यह काम कर गया।",
        "neutral": "जानकारी के लिए धन्यवाद। मैं इसे देख रहा हूँ।",
        "negative": "असुविधा के लिए खेद है। मैं आपकी समस्या देख रहा हूँ।",
        "frustrated": "मैं आपकी नाराज़गी समझता हूँ और खेद व्यक्त करता हूँ। मैं इसे अभी उठा रहा हूँ।",
        "urgent": "मैंने इसे अत्यावश्यक के रूप में चिह्नित किया है और विशेषज्ञ अभी इसे देख रहे हैं।",
        "sarcastic": "मैं समझता हूँ, और खेद है कि यह बार-बार हो रहा है। मैं इसे देख रहा हूँ।",
    },
    "ta": {
        "positive": "இது வேலை செய்ததில் மகிழ்ச்சி.",
        "neutral": "விவரங்களுக்கு நன்றி. நான் இதைப் பார்க்கிறேன்.",
        "negative": "சிரமத்திற்கு மன்னிக்கவும். உங்கள் பிரச்சினையை நான் பார்க்கிறேன்.",
        "frustrated": "உங்கள் எரிச்சலைப் புரிந்துகொள்கிறேன், மன்னிக்கவும். இதை இப்போது எடுத்துக்கொள்கிறேன்.",
        "urgent": "இதை அவசரமாகக் குறித்துள்ளேன், நிபுணர் இப்போது பார்க்கிறார்.",
        "sarcastic": "புரிகிறது, இது திரும்பத் திரும்ப நடப்பதற்கு மன்னிப்பு. நான் இதில் இருக்கிறேன்.",
    },
    "hinglish": {
        "positive": "Achha laga ki yeh kaam kar gaya.",
        "neutral": "Details ke liye thanks. Main ise dekh raha hoon.",
        "negative": "Trouble ke liye sorry. Main aapki problem dekh raha hoon.",
        "frustrated": "Main aapki frustration samajhta hoon, sorry. Main ise abhi utha raha hoon.",
        "urgent": "Maine ise urgent mark kiya hai, specialist abhi dekh raha hai.",
        "sarcastic": "Samajh gaya, aur sorry ki yeh baar-baar ho raha hai. Main is par hoon.",
    },
    "tanglish": {
        "positive": "Idhu work aachu, santhosham.",
        "neutral": "Details-ku thanks. Naan idha paakaren.",
        "negative": "Kashtathuku sorry. Unga problem-a naan paakaren.",
        "frustrated": "Unga frustration puriyudhu, sorry. Idha ippo eduthukkaren.",
        "urgent": "Idha urgent-a mark panniten, specialist ippo paakuraanga.",
        "sarcastic": "Puriyudhu, idhu thirumba thirumba nadakkurathuku sorry. Naan idhula iruken.",
    },
}

ESCALATED = {
    "en": "I've escalated this to our {queue} team.",
    "hi": "मैंने इसे हमारी {queue} टीम को भेज दिया है।",
    "ta": "இதை எங்கள் {queue} குழுவுக்கு அனுப்பியுள்ளேன்.",
    "hinglish": "Maine ise hamari {queue} team ko bhej diya hai.",
    "tanglish": "Idha enga {queue} team-ku anuppiten.",
}

COMPLAINT_TONES = {"negative", "frustrated", "urgent", "sarcastic"}


def _language(language: str | None) -> str:
    return language if language in LANGUAGES else "en"


def compose(
    triage: dict,
    policy_result: dict | None = None,
    escalation: dict | None = None,
) -> dict:
    """Build the customer-facing reply for one turn."""
    language = _language(triage.get("language"))
    label = str(triage.get("sentiment", {}).get("label", "neutral"))
    tone = str(triage.get("sentiment", {}).get("tone", "professional"))

    if policy_result and policy_result.get("status") == "answered":
        body = str(policy_result["answer"])
        if label in COMPLAINT_TONES:
            body = f"{ACK[language].get(label, '')} {body}".strip()
    else:
        body = ACK[language].get(label, ACK[language]["neutral"])

    reply = body
    if escalation:
        queue = str(escalation.get("queue") or "support").replace("_", " ")
        reply = f"{reply} {ESCALATED[language].format(queue=queue)}".strip()

    return {"reply": reply, "language": language, "tone": tone}
