"""Personal data is redacted with Presidio in LOGS and TRACES only (S3).

The model still reads the real email (it needs the customer's name to do its job; Groq is the processor
named in the SOW). What we keep, print and send to Langfuse is redacted: names, email addresses, phone
numbers, IBANs, card numbers and IP addresses become <PERSON>, <EMAIL_ADDRESS>...
"""

import json
from datetime import UTC, datetime
from functools import cache
from typing import Any

ENTITIES = ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD", "IP_ADDRESS"]


@cache
def _engines() -> tuple[Any, Any]:
    import tldextract  # type: ignore[import-not-found]

    # Presidio's email recognizer uses tldextract, which downloads the public-suffix list on first use. The agent
    # has no internet (by design), so use the copy bundled with tldextract instead of trying and failing.
    tldextract.tldextract.TLD_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    # ponytail: the small English spaCy model; names in German/Spanish/Hindi text are caught less often.
    # Upgrade: a multilingual model (xx_ent_wiki_sm) or one model per reply_language from S4.
    config = {"nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}
    nlp = NlpEngineProvider(nlp_configuration=config).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])
    # The built-in email recognizer only accepts public top-level domains; private ones such as .local (all of this
    # lab's mail, and many company-internal domains) would slip through. A plain pattern catches them too.
    any_domain = Pattern("email", r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", 0.9)
    analyzer.registry.add_recognizer(
        PatternRecognizer(supported_entity="EMAIL_ADDRESS", name="AnyDomainEmail", patterns=[any_domain])
    )
    return analyzer, AnonymizerEngine()


def redact(text: str) -> str:
    if not text.strip():
        return text
    analyzer, anonymizer = _engines()
    found = analyzer.analyze(text=text, language="en", entities=ENTITIES)
    return str(anonymizer.anonymize(text=text, analyzer_results=found).text)


def log(event: str, **fields: Any) -> None:
    """One JSON line per event on stdout (Docker Desktop -> Logs). Every string value is redacted."""
    clean = {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()}
    print(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **clean},
                     ensure_ascii=False, default=str), flush=True)  # fmt: skip


if __name__ == "__main__":  # self-check: python -m apex_agent.security.pii
    out = redact("Call John Miller on +1 312 555 0147 or mail john.miller@kestrel.example or kestrelautomation@customers.local,"
                 " IBAN DE89370400440532013000.")  # fmt: skip
    print(out)
    for leak in ("John Miller", "john.miller@", "kestrelautomation@", "555 0147", "DE8937"):
        assert leak not in out, (leak, out)
