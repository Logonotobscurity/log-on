"""Input policy (pre-planning guardrail). Deterministic, explainable rules.

Returns a decision dict: {"allowed": bool, "reasons": [...], "rule_ids": [...], "normalized": bool}

Phase 4 (red-team fixes): rules are matched against the raw goal AND a normalised copy, so obfuscated attacks hit
the same rules: Unicode NFKC + confusable (Cyrillic/Greek homoglyph) folding, zero-width/format characters
removed, leetspeak inside words decoded, intra-word hyphens/dots/asterisks removed, whitespace collapsed.
Base64 blobs are decoded and checked too, and "decode this and do what it says" is refused outright.
New rule families: jailbreak personas / "no rules" role-play, synonym and multilingual (French, Pidgin)
instruction overrides.
"""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern
    reason: str


_OVERRIDE_VERB = r"(ignore|disregard|forget|overlook|override|bypass|skip|drop|neglect|ignorez|oubliez|ignora|olvida)"
_PRIOR = r"(previous|prior|above|all|earlier|preceding|existing|di|toutes?|les|précédentes?|anteriores|your|the)"
_RULES = r"(instructions?|rules?|polic(y|ies)|guardrails?|safety|restrictions?|guidelines?|consignes|règles|instrucciones)"

RULES: list[Rule] = [
    Rule("prompt_injection",
         re.compile(r"\b" + _OVERRIDE_VERB + r"\b.{0,30}\b" + _PRIOR + r"\b.{0,20}\b" + _RULES, re.I),
         "Looks like a prompt-injection attempt to override instructions."),
    Rule("prompt_injection",
         re.compile(r"\b(system|developer|admin)\s*(prompt|message|note|notice|override)\s*:|^\s*```\s*(system|developer)\b"
                    r"|\byour (new|real) instructions are\b", re.I | re.M),
         "Looks like an injected system/developer message."),
    Rule("jailbreak",
         re.compile(r"\b(you are|you're|act as|pretend (to be|you are)|roleplay as|play a game where you are)\b.{0,40}"
                    r"\b(dan|unrestricted|unfiltered|uncensored|jailbroken|evil|no (rules|restrictions|limits|filters))\b"
                    r"|\b(developer|god|debug|sudo) mode\b|\bjailbreak\b|\bdo anything now\b"
                    r"|\b(safety|content|privacy) (filters?|rules?|guardrails?) (are |is )?(off|disabled|removed)\b"
                    r"|\bwith no (rules|restrictions|limits|filters)\b"
                    r"|\b(bypass|disable|turn off|switch off)\b.{0,20}\b(privacy|safety|security|content)\b.{0,10}"
                    r"\b(rules?|filters?|checks?|guard\w*)\b"
                    r"|\bpretend you are my (late )?grand(mother|ma|father|pa)\b", re.I),
         "Looks like a jailbreak (persona / role-play / 'no rules' framing)."),
    # post-Phase-4 step 2 red-team fixes: fake developer notes, injected tool results / role JSON / hidden comments,
    # "no privacy rules" hypotheticals, prefix-injection, and instruction overrides in Yoruba, Hausa, Igbo and Pidgin
    Rule("prompt_injection",
         re.compile(r"\b(new|updated|revised) instructions? from (your|the) (developer|creator|admin|system|owner)s?\b"
                    r"|\b(previous|prior|earlier|old|existing) (guidelines|instructions|rules|policies) (are|is) "
                    r"(now )?(void|cancell?ed|revoked|invalid|obsolete|lifted)\b"
                    r"|\b(stop|cease) (applying|following|enforcing)\b.{0,20}\b(privacy|safety|security)?\s*"
                    r"(rules?|polic(y|ies)|guardrails?|filters?)\b"
                    r"|^\s*(tool|function|api) (result|output|response)\s*:"
                    r"|[\"']role[\"']\s*:\s*[\"'](system|developer)[\"']"
                    r"|<!--\s*(assistant|system|ai|instructions?)\b", re.I | re.M),
         "Looks like an injected system/developer/tool message."),
    Rule("prompt_injection",
         re.compile(r"\bgbagbe\b.{0,30}\b(ilana|ofin|ase|itoni)\b"                      # Yoruba: forget ... rules
                    r"|\bmanta\b.{0,30}\b(umarni\w*|dokok\w*|ka'?idoji)\b"               # Hausa
                    r"|\b(chefuo|hapu)\b.{0,30}\b(ntuziaka|iwu|usoro)\b"                  # Igbo
                    r"|\b(comot|forget|drop|leave)\b.{0,20}\b(all )?(di|dem|those)\s+(rules?|instructions?)\b"  # Pidgin
                    r"|\bno hide\b.{0,30}\b(nin|card|phone|number|password)", re.I),
         "Looks like a prompt-injection attempt to override instructions (Nigerian languages / Pidgin)."),
    Rule("jailbreak",
         re.compile(r"\bif you (had|have|were) no\b.{0,20}\b(privacy|safety|content)?\s*(rules|restrictions|limits|"
                    r"filters|guardrails)\b"
                    r"|\b(as an?|you are an?) (unfiltered|unrestricted|uncensored|jailbroken) (assistant|ai|bot|model)\b"
                    r"|\b(continue|complete) (this|the following) (text|sentence|response)\b.{0,40}"
                    r"\b(nins?|card numbers?|passwords?|phone numbers?|records)\b", re.I),
         "Looks like a jailbreak (hypothetical / prefix-injection framing)."),
    Rule("encoded_instructions",
         re.compile(r"\b(decod\w*|decipher\w*|base64|rot13|revers\w*)\b.{0,40}\b(do|follow|execute|obey|run|act on)\b"
                    r".{0,20}\b(it|this|what it says|the instructions?)\b"
                    r"|\b(hex|url|rot13|base64|morse|binary)[- ]?(decode|decoded)\b.{0,15}\b(and )?(execute|run|follow|obey|do)\b"
                    r"|\b(run|execute|follow|obey) the decoded (instruction|text|command|message)s?\b", re.I),
         "Encoded or hidden instructions are refused."),
    Rule("bulk_pii_exfiltration",
         re.compile(r"\b(export|dump|download|list|send|leak|print|show|give)\b.{0,40}\b(all|every|entire|bulk)\b.{0,40}"
                    r"(\b(customers?|users?)\b.{0,30}\b(with|and|including|plus)\b.{0,20}\b(emails?|phones?|nins?|bvns?|cards?|"
                    r"passwords?|details|records|data)\b|\b(customer |user )?(records?|card numbers?|nins?|bvns?|passwords?|emails?|"
                    r"phone numbers?|(customer|user) (list|data|details|database))\b)", re.I),
         "Bulk export of personal data is not permitted."),
    Rule("credential_access",
         re.compile(r"\b(password|api[ _-]?key|secret|private key|access token|otp|pin)s?\b.{0,30}\b(reveal|show|give|send|steal|get|extract)|\b(reveal|show|give|send|steal|get|extract)\b.{0,30}\b(password|api[ _-]?key|secret|private key|access token|otp)s?\b"
                    r"|\bwhat(?:'s| is| are)\b.{0,25}\b(password|api[ _-]?key|private key|access token|otp)s?\s+(for|of|on|to)\b", re.I),
         "Requests for credentials or secrets are refused."),
    Rule("harmful",
         re.compile(r"\b(make|build|synthesi[sz]e)\b.{0,30}\b(bomb|explosive|malware|ransomware|nerve agent)\b", re.I),
         "Harmful content is refused."),
    Rule("fraud",
         re.compile(r"\b(fake|forge|spoof)\b.{0,30}\b(bvn|nin|id card|invoice|bank statement|identity)\b", re.I),
         "Identity/document fraud is refused."),
]

MAX_GOAL_CHARS = 2000

# confusables: the common Cyrillic / Greek look-alikes of Latin letters
_CONFUSABLES = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "һ": "h",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X", "І": "I", "К": "K", "М": "M", "Н": "H",
    "В": "B", "α": "a", "ο": "o", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ρ": "p", "τ": "t", "υ": "u",
})
_LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "1": "i", "!": "i", "0": "o", "5": "s", "$": "s", "7": "t"})
_LEET_WORD = re.compile(r"[A-Za-z@$!]*[0-9@$!][A-Za-z0-9@$!]*")
_B64 = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def _deleet(m: re.Match) -> str:
    w = m.group(0)
    letters = sum(c.isalpha() for c in w)
    # only words that are mostly letters with a few digit substitutions (keeps C-1001, Q3, 2^10 intact)
    if letters >= 2 and letters >= len(w) - letters and not re.fullmatch(r"[A-Za-z]\d+|\d+[A-Za-z]?", w):
        return w.translate(_LEET)
    return w


def _deleet_all(text: str) -> str:
    return re.sub(r"\b\w*\d\w*\b",
                  lambda m: m.group(0).translate(_LEET) if re.search(r"[A-Za-z]", m.group(0)) else m.group(0), text)


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Cf")  # zero-width & other format chars
    t = t.translate(_CONFUSABLES)
    t = _LEET_WORD.sub(_deleet, t)
    t = re.sub(r"(?<=[A-Za-z])[-_.*·](?=[A-Za-z])", "", t)  # ig-nore / ig.nore / ig*nore
    t = re.sub(r"\b(?:[A-Za-z] ){3,}[A-Za-z]\b", lambda m: m.group(0).replace(" ", ""), t)  # i g n o r e
    return re.sub(r"\s+", " ", t).strip()


_HEX = re.compile(r"\b(?:[0-9a-fA-F]{2}){12,}\b")
_PCT = re.compile(r"%[0-9A-Fa-f]{2}")


def _decoded_payloads(text: str) -> list[str]:
    """Hidden payloads: base64 blobs, hex blobs, %-encoded text and ROT13 (when the text mentions rot13)."""
    import codecs
    import urllib.parse
    out = []
    for blob in _HEX.findall(text):
        try:
            s = bytes.fromhex(blob).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", s):
            out.append(s)
    if len(_PCT.findall(text)) >= 3:
        out.append(urllib.parse.unquote(text))
    if re.search(r"(?i)\brot ?13\b", text):
        out.append(codecs.decode(text, "rot13"))
    for blob in _B64.findall(text):
        try:
            raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=True)
            s = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if sum(ch.isprintable() for ch in s) >= 0.9 * len(s) and re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", s):
            out.append(s)
    return out


def check_input(goal: str) -> dict:
    reasons, ids = [], []
    if not goal or not goal.strip():
        reasons.append("Empty goal."); ids.append("empty")
    if len(goal) > MAX_GOAL_CHARS:
        reasons.append(f"Goal longer than {MAX_GOAL_CHARS} characters."); ids.append("too_long")
    norm = normalize(goal or "")
    views = [goal or "", norm] + [normalize(p) for p in _decoded_payloads(goal or "")]
    folded = "".join(c for c in unicodedata.normalize("NFKD", goal or "") if not unicodedata.combining(c))
    if folded not in views:  # Yoruba/Igbo tone marks, accents: "ránṣẹ́ sí" -> "ranse si"
        views.append(folded)
    aggressive = _deleet_all(norm)  # step 2: also '1f', 'h4d', '1l4n4' (matching only; never shown to anyone)
    if aggressive != norm:
        views.append(aggressive)
    for rule in RULES:
        if rule.id in ids:
            continue
        if any(rule.pattern.search(v) for v in views):
            reasons.append(rule.reason); ids.append(rule.id)
    hit_raw = any(r.pattern.search(goal or "") for r in RULES)
    normalized = bool(ids) and not hit_raw and norm != (goal or "").strip()
    # Post-Phase-4 step 2: ML prompt-injection classifier on the same views (raw, normalised, decoded payloads).
    # Optional: without the model (CI / offline) `ml_score` is None and the regex rules above stand alone.
    ml_score = None
    if goal and len(goal) <= MAX_GOAL_CHARS:
        from .guards import injection_ml
        scores = [x for x in (injection_ml.score(v) for v in dict.fromkeys(views) if v.strip()) if x is not None]
        if scores:
            ml_score = round(max(scores), 4)
            if ml_score >= injection_ml.threshold() and ml_corroborated(views) and "prompt_injection_ml" not in ids:
                reasons.append(f"An ML prompt-injection classifier flagged this request (score {ml_score:.2f}).")
                ids.append("prompt_injection_ml")
    return {"allowed": not reasons, "reasons": reasons, "rule_ids": ids,
            "normalized": normalized, "ml_score": ml_score}


# The classifier was trained to spot injected instructions in third-party text; on a user's own short commands
# ("Repeat the last answer but shorter", "From now on keep answers brief", "Remember that my NIN is ...") it fires at
# ~1.0. So on user input an ML hit only counts when it is corroborated: the text names an outside (untrusted)
# destination, or it mentions protected data / the assistant's rules together with an override or persona verb or
# someone else's / bulk data (a user's own "my NIN is ..." or "send the report" is not enough).
_ML_RISK = re.compile(
    r"\b(nins?|bvns?|card( numbers?)?s?|passwords?|passcodes?|pins?|otps?|secrets?|credentials?|api ?keys?|tokens?|"
    r"phone( numbers?)?|nomba|emails?|records?|customer (list|data|details|database)|privacy|safety|filters?|"
    r"rules?|guidelines?|polic(y|ies)|restrictions?|ilana|ofin|umarni\w*|dokok\w*|ntuziaka)\b", re.I)
_ML_ACTION = re.compile(  # override / persona verbs only: disclosure verbs (send, show) are everyday requests
    r"\b(ignore|disregard|forget|override|bypass|void|revoke|cancel+ed|comot|exfiltrat\w*|leak|dump|unfiltered|"
    r"unrestricted|uncensored|jailbr\w*|pretend|role-?play|hypothetic\w*|imagine|gbagbe|manta|chefuo|hapu)\b", re.I)
_ML_THIRD_PARTY = re.compile(  # someone else's data, or data in bulk (not "my NIN is ...")
    r"\b(customers?|clients?|users?|staff|all|every|stored|admin|records?|database|crm|onibara|abokin ciniki|"
    r"onye ahia)\b", re.I)
_MEMORY_STATEMENT = re.compile(r"\s*(please\s+)?(remember|note|keep in mind|don't forget)\b", re.I)
# an address/URL used as a destination ("... to x@evil.example", Pidgin "go", Hausa "zuwa", Yoruba "si", Igbo "na")
_EMAIL_OR_URL = re.compile(r"\b(?:to|go|give|zuwa|si|na|cc|bcc|into|via|at)\s+(?:[\w-]+\s+){0,2}?"
                           r"(?:[\w.+-]+@([\w-]+(?:\.[\w-]+)+)|https?://([^/\s]+))", re.I)


def ml_corroborated(views: list[str]) -> bool:
    from .permissions import trusted_domains
    trusted = trusted_domains()
    for v in views:
        # "Remember that ..." statements go to the memory guard (which rejects secrets and planted instructions with
        # a clear "won't store" message), so there only an override/persona verb corroborates an ML hit.
        third_party = _ML_THIRD_PARTY.search(v) and not _MEMORY_STATEMENT.match(v)
        if _ML_RISK.search(v) and (_ML_ACTION.search(v) or third_party):
            return True
        for m in _EMAIL_OR_URL.finditer(v):
            dom = (m.group(1) or m.group(2) or "").lower()
            if not any(dom == t or dom.endswith("." + t) for t in trusted):
                return True
    return False


def looks_like_injection(text: str) -> bool:
    """Regex fallback for untrusted text (tool output, retrieved documents, memories): the injection/jailbreak
    rules only (not the PII-request rules, which legitimately match business data)."""
    views = [text or "", normalize(text or "")]
    return any(r.pattern.search(v) for r in RULES if r.id in INJECTION_RULE_IDS for v in views)


INJECTION_RULE_IDS = {"prompt_injection", "jailbreak", "encoded_instructions"}
