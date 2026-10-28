"""Short fixed system messages in the languages our owners use.

Anything the agent *writes* is generated in the owner's language by the model; these
are only the deterministic strings (onboarding, errors, button feedback).
"""

from __future__ import annotations

STRINGS: dict[str, dict[str, str]] = {
    "welcome": {
        "en": "Welcome to DukaanPilot, {owner}! I'm connected to {shop}. Ask me about sales or stock, or send a voice note.",
        "hi": "DukaanPilot में स्वागत है, {owner}! मैं {shop} से जुड़ गया हूँ। बिक्री या स्टॉक पूछिए, या वॉइस नोट भेजिए।",
        "te": "DukaanPilot కి స్వాగతం, {owner}! నేను {shop} కి కనెక్ట్ అయ్యాను. అమ్మకాలు లేదా స్టాక్ గురించి అడగండి, లేదా వాయిస్ నోట్ పంపండి.",
    },
    "not_registered": {
        "en": "I don't know this chat yet. Send /start followed by your shop code, for example: /start DEMO-KIRANA",
        "hi": "मैं इस चैट को अभी नहीं पहचानता। /start के बाद अपनी दुकान का कोड भेजिए, जैसे: /start DEMO-KIRANA",
        "te": "ఈ చాట్ నాకు ఇంకా తెలియదు. /start తర్వాత మీ షాప్ కోడ్ పంపండి, ఉదా: /start DEMO-KIRANA",
    },
    "bad_code": {
        "en": "That shop code was not found. Please check it and try again.",
        "hi": "यह दुकान कोड नहीं मिला। कृपया जाँचकर फिर से कोशिश करें।",
        "te": "ఆ షాప్ కోడ్ దొరకలేదు. దయచేసి సరిచూసి మళ్ళీ ప్రయత్నించండి.",
    },
    "voice_too_long": {
        "en": "That voice note is too long. Please keep it under {seconds} seconds.",
        "hi": "यह वॉइस नोट बहुत लंबा है। कृपया इसे {seconds} सेकंड से छोटा रखें।",
        "te": "ఈ వాయిస్ నోట్ చాలా పొడవుగా ఉంది. దయచేసి {seconds} సెకన్ల లోపు ఉంచండి.",
    },
    "error": {
        "en": "Sorry, something went wrong on my side. Please try again in a moment.",
        "hi": "माफ़ कीजिए, मेरी तरफ़ से कुछ गड़बड़ हुई। कृपया थोड़ी देर में फिर कोशिश करें।",
        "te": "క్షమించండి, నా వైపు ఏదో తప్పు జరిగింది. కొంత సేపటి తర్వాత మళ్ళీ ప్రయత్నించండి.",
    },
    "slow_down": {
        "en": "You're sending messages very fast. Please wait a minute.",
        "hi": "आप बहुत तेज़ संदेश भेज रहे हैं। कृपया एक मिनट रुकिए।",
        "te": "మీరు చాలా వేగంగా సందేశాలు పంపుతున్నారు. దయచేసి ఒక నిమిషం ఆగండి.",
    },
    "approved": {
        "en": "Order {id} approved. Tap below to send it to {supplier} on WhatsApp.",
        "hi": "ऑर्डर {id} मंज़ूर हुआ। {supplier} को WhatsApp पर भेजने के लिए नीचे टैप कीजिए।",
        "te": "ఆర్డర్ {id} ఆమోదించబడింది. {supplier} కి WhatsApp లో పంపడానికి కింద నొక్కండి.",
    },
    "rejected": {
        "en": "Order {id} cancelled. Nothing was sent.",
        "hi": "ऑर्डर {id} रद्द किया गया। कुछ नहीं भेजा गया।",
        "te": "ఆర్డర్ {id} రద్దు చేయబడింది. ఏమీ పంపబడలేదు.",
    },
    "draft_missing": {
        "en": "I can't find that order any more.",
        "hi": "वह ऑर्डर अब नहीं मिल रहा।",
        "te": "ఆ ఆర్డర్ ఇప్పుడు కనబడటం లేదు.",
    },
    "draft_done": {
        "en": "That order was already handled.",
        "hi": "वह ऑर्डर पहले ही निपटाया जा चुका है।",
        "te": "ఆ ఆర్డర్ ఇప్పటికే పూర్తయింది.",
    },
    "photo_soon": {
        "en": "Photo bills are coming soon. For now, send the items as text or a voice note.",
        "hi": "फ़ोटो से बिल पढ़ना जल्द आ रहा है। अभी सामान टेक्स्ट या वॉइस नोट में भेजिए।",
        "te": "ఫోటో బిల్లులు త్వరలో వస్తాయి. ప్రస్తుతానికి సరుకులను టెక్స్ట్ లేదా వాయిస్ నోట్ లో పంపండి.",
    },
    "no_pending": {
        "en": "You have no orders waiting for approval.",
        "hi": "मंज़ूरी के लिए कोई ऑर्डर बाकी नहीं है।",
        "te": "ఆమోదం కోసం ఎలాంటి ఆర్డర్లు వేచి లేవు.",
    },
    "approve_btn": {"en": "Approve", "hi": "मंज़ूर करें", "te": "ఆమోదించు"},
    "reject_btn": {"en": "Cancel", "hi": "रद्द करें", "te": "రద్దు చేయి"},
    "send_wa_btn": {"en": "Send on WhatsApp", "hi": "WhatsApp पर भेजें", "te": "WhatsApp లో పంపండి"},
}


def t(key: str, lang: str, **fmt: object) -> str:
    table = STRINGS[key]
    text = table.get(lang) or table["en"]
    return text.format(**fmt) if fmt else text
