"""Microsoft Presidio PII detection behind the existing redactor/validator interfaces (post-Phase-4 step 2).

    LOG_ON_PII_BACKEND = combined (default) | regex | presidio
      combined  the Phase 1-4 regex redactors first (stable, explainable validator names), then Presidio on what is
                left (context-aware scores, Nigerian recognizers, IBAN/IP/crypto, optional PERSON). Falls back to regex-only, with a one-time
                warning, when presidio-analyzer / the spaCy model are not installed (CI, offline).
      regex     Phase 1-4 behaviour only.
      presidio  Presidio only (for comparison; not recommended).
    LOG_ON_PII_PERSON = 1        also redact person names (spaCy NER). Off by default: customer and supplier
                                 names are business data in this app, and the small model's NER is noisy.
    LOG_ON_PII_SPACY_MODEL       spaCy model (default en_core_web_sm; installed as a pip package, not in the repo).

Custom recognizers (all fictional examples in tests): Nigerian NIN (11 digits, labelled or bare), BVN (11 digits,
"BVN" context), Nigerian phones (0XXX XXX XXXX, +234 / 234 / +234 (0) forms, spaced, dashed, dotted, bracketed)
and payment cards (Presidio's Luhn-checked recognizer). Detected spans become the same tags the regex layer uses:
<EMAIL> <PHONE> <NIN> <BVN> <CARD> <IBAN> <IP> <CRYPTO> <PERSON>.
"""
from __future__ import annotations

import functools
import logging
import os
import re
import threading

from .base import FailResult, PassResult, Validator

log = logging.getLogger(__name__)

TAGS = {"EMAIL_ADDRESS": "EMAIL", "CREDIT_CARD": "CARD", "PHONE_NUMBER": "PHONE", "NG_PHONE": "PHONE",
        "NG_NIN": "NIN", "NG_BVN": "BVN", "IBAN_CODE": "IBAN", "IP_ADDRESS": "IP", "CRYPTO": "CRYPTO",
        "PERSON": "PERSON"}
BASE_ENTITIES = ["EMAIL_ADDRESS", "CREDIT_CARD", "PHONE_NUMBER", "NG_PHONE", "NG_NIN", "NG_BVN", "IBAN_CODE",
                 "IP_ADDRESS", "CRYPTO"]
SCORE_THRESHOLD = 0.5
# Nigerian mobile prefixes 070x/080x/081x/090x/091x (+ 0702x etc. are covered by the digit class)
_NG_LOCAL = r"0[789][01]\d"
_SEP = r"[ .\-]?"
NG_PHONE_PATTERNS = [
    ("ng_phone_intl", r"(?<![\w+])\+?234" + _SEP + r"(?:\(0\)" + _SEP + r")?[789][01]\d" + r"(?:" + _SEP + r"\d){7}(?!\d)", 0.85),
    ("ng_phone_local", r"(?<![\w+])\(?" + _NG_LOCAL + r"\)?" + r"(?:" + _SEP + r"\d){7}(?!\d)", 0.7),
]
NIN_PATTERNS = [
    ("nin_spaced", r"(?<!\d)\d{4}[ -]\d{3}[ -]\d{4}(?!\d)", 0.3),
    ("nin_bare", r"(?<![\d+])(?!0[789][01]\d{8}\b)\d{11}(?!\d)", 0.45),
]
BVN_PATTERNS = [("bvn", r"(?<![\d+])2\d{10}(?!\d)", 0.2)]
NIN_CONTEXT = ["nin", "national", "identification", "identity", "nimc", "id number", "slip"]
BVN_CONTEXT = ["bvn", "bank verification", "verification number"]
PHONE_CONTEXT = ["phone", "call", "mobile", "tel", "whatsapp", "number", "sms", "reach"]

_lock = threading.Lock()
_engine = None
_unavailable: str | None = None


def backend() -> str:
    return os.getenv("LOG_ON_PII_BACKEND", "combined").lower()


def person_enabled() -> bool:
    return os.getenv("LOG_ON_PII_PERSON", "0").lower() in ("1", "true", "yes", "on")


def _build():
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (CreditCardRecognizer, CryptoRecognizer, EmailRecognizer,
                                                          IbanRecognizer, IpRecognizer, PhoneRecognizer)
    model = os.getenv("LOG_ON_PII_SPACY_MODEL", "en_core_web_sm")
    nlp = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": model}]}).create_engine()
    if not person_enabled():  # only tokens + lemmas are needed for context words: drop the parser and NER (faster)
        import spacy
        nlp.nlp["en"] = spacy.load(model, exclude=["parser", "ner"])
    reg = RecognizerRegistry(supported_languages=["en"])
    for r in (EmailRecognizer(), CreditCardRecognizer(), IbanRecognizer(), IpRecognizer(), CryptoRecognizer(),
              PhoneRecognizer(supported_regions=("NG", "GB", "US"), context=PHONE_CONTEXT)):
        reg.add_recognizer(r)
    reg.add_recognizer(PatternRecognizer("NG_PHONE", name="NigerianPhoneRecognizer", context=PHONE_CONTEXT,
                                         patterns=[Pattern(n, p, s) for n, p, s in NG_PHONE_PATTERNS]))
    reg.add_recognizer(PatternRecognizer("NG_NIN", name="NigerianNINRecognizer", context=NIN_CONTEXT,
                                         patterns=[Pattern(n, p, s) for n, p, s in NIN_PATTERNS]))
    reg.add_recognizer(PatternRecognizer("NG_BVN", name="NigerianBVNRecognizer", context=BVN_CONTEXT,
                                         patterns=[Pattern(n, p, s) for n, p, s in BVN_PATTERNS]))
    if person_enabled():
        from presidio_analyzer.predefined_recognizers import SpacyRecognizer
        reg.add_recognizer(SpacyRecognizer(supported_entities=["PERSON"]))
    return AnalyzerEngine(nlp_engine=nlp, registry=reg, supported_languages=["en"])


def get_analyzer():
    """The shared AnalyzerEngine, or None if Presidio / the spaCy model is unavailable (never raises)."""
    global _engine, _unavailable
    if _engine is not None or _unavailable is not None:
        return _engine
    with _lock:
        if _engine is None and _unavailable is None:
            try:
                _engine = _build()
            except Exception as exc:  # noqa: BLE001 - missing optional deps -> regex fallback
                _unavailable = f"{type(exc).__name__}: {exc}"
                log.warning("Presidio unavailable, using regex PII redaction only (%s)", _unavailable)
    return _engine


def available() -> bool:
    return get_analyzer() is not None


def status() -> dict:
    get_analyzer()
    return {"backend": backend(), "presidio": _engine is not None, "error": _unavailable,
            "person": person_enabled()}


def _entities() -> list[str]:
    return BASE_ENTITIES + (["PERSON"] if person_enabled() else [])


_CARD_SHAPE = re.compile(r"(?<![\d-])(?:\d{13,19}|\d{4}(?:[ -]\d{4}){2}[ -]\d{1,7}|\d{4}[ -]\d{6}[ -]\d{5})(?![\d-])")


def _plausible(r, text: str) -> bool:
    """Presidio's card pattern accepts any digit run with separators, so 'NIN 12345678901 4111' can pass Luhn by chance.
    Only accept real card layouts: contiguous 13-19 digits, 4-4-4-x groups, or Amex 4-6-5."""
    if r.entity_type == "CREDIT_CARD":
        return bool(_CARD_SHAPE.fullmatch(text[r.start:r.end].strip()))
    return True


def _luhn(digits: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(digits)):
        d = int(c) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def _resolve(results, text: str = "") -> list[tuple[int, int, str]]:
    """Highest score wins on overlaps; BVN beats NIN when both match the same digits (context decides)."""
    pri = {"NG_BVN": 3, "CREDIT_CARD": 3, "EMAIL_ADDRESS": 3, "IBAN_CODE": 3, "NG_PHONE": 2, "PHONE_NUMBER": 1,
           "NG_NIN": 1}
    cands = []
    for r in results:
        if text and not _plausible(r, text):
            # implausible card span (e.g. '12345678901 4111'): look for a real card layout right next to it
            lo, hi = max(0, r.start - 24), min(len(text), r.end + 24)
            for m in _CARD_SHAPE.finditer(text, lo, hi):
                if m.start() < r.end and r.start < m.end() and _luhn(re.sub(r"\D", "", m.group())):
                    cands.append((r.score, pri["CREDIT_CARD"], m.start(), m.end(), "CARD"))
            continue
        cands.append((r.score, pri.get(r.entity_type, 0), r.start, r.end, TAGS.get(r.entity_type, r.entity_type)))
    taken: list[tuple[int, int, str]] = []
    for score, p, start, end, tag in sorted(cands, key=lambda c: (-(c[0] + 0.01 * c[1]), c[2])):
        if score < SCORE_THRESHOLD or any(start < e and s < end for s, e, _ in taken):
            continue
        taken.append((start, end, tag))
    return sorted(taken)


def find(text: str) -> list[tuple[int, int, str]]:
    eng = get_analyzer()
    if eng is None or not text:
        return []
    if not person_enabled() and not re.search(r"[\d@]", text):
        return []  # every enabled entity needs a digit or an @: skip the NLP pass
    return _resolve(eng.analyze(text=text, language="en", entities=_entities(),
                                score_threshold=SCORE_THRESHOLD * 0.5), text)


_anonymizer = None


def _anonymize(text: str, spans: list[tuple[int, int, str]]) -> str:
    """presidio-anonymizer "replace" operator, one <TAG> per entity type (manual splice if it is not installed)."""
    global _anonymizer
    try:
        from presidio_anonymizer import AnonymizerEngine
        from presidio_anonymizer.entities import OperatorConfig, RecognizerResult
        if _anonymizer is None:
            _anonymizer = AnonymizerEngine()
        res = [RecognizerResult(entity_type=tag, start=s, end=e, score=1.0) for s, e, tag in spans]
        ops = {tag: OperatorConfig("replace", {"new_value": f"<{tag}>"}) for _, _, tag in spans}
        return _anonymizer.anonymize(text=text, analyzer_results=res, operators=ops).text
    except ImportError:
        out, last = [], 0
        for s, e, tag in spans:
            out.append(text[last:s]); out.append(f"<{tag}>"); last = e
        out.append(text[last:])
        return "".join(out)


@functools.lru_cache(maxsize=8192)
def redact(text: str) -> str:
    """Replace Presidio findings with <TAG>s (cached: the audit log and tracer re-redact the same strings)."""
    spans = find(text)
    return _anonymize(text, spans) if spans else text


class PresidioRedactor(Validator):
    """Output-guard validator (OnFail.FIX): Presidio findings are redacted, like the regex redactors."""
    name, label = "presidio_pii", "PII"

    def validate(self, value, metadata=None):
        spans = find(value)
        if not spans:
            return PassResult()
        tags = sorted({t for _, _, t in spans})
        return FailResult(error_message=f"{len(spans)} PII value(s) found by Presidio ({', '.join(tags)})",
                          fix_value=redact(value))
