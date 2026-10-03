"""A deliberately *obedient* stand-in model for the Attack Lab.

``NoticeParserModelClient`` only falls for one phrase ("PAY INSTEAD TO:").
The Attack Lab needs a model that is fooled by every attack in its library,
and by most text a visitor types, so that each defence layer is tested
against an agent that really is compromised. This class models the worst
case: it reads text a human can't see (zero-width characters, full-width
look-alikes) and obeys any instruction to send money somewhere else, or to
pay a different total.

It is not an approximation of Gemini. A real model may resist some of
these attacks. The point of the lab is that PayGuard's guarantee must not
depend on that.
"""

from __future__ import annotations

import re
import unicodedata

from services.runner.model_client import ModelTurn

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))

_CURRENCY = {"lkr": "LKR", "rs": "LKR", "rs.": "LKR", "usd": "USD", "$": "USD", "eur": "EUR", "\u20ac": "EUR",
             "sek": "SEK", "kr": "SEK"}
_NUM = r"(\d[\d,]*(?:\.\d{1,2})?|\d{1,3}(?:\s\d{3})+(?:,\d{2})?)"
_AMOUNT = r"(LKR|Rs\.?|USD|\$|EUR|\u20ac|SEK)\s*" + _NUM
_AMOUNT_RE = re.compile(_AMOUNT, re.IGNORECASE)
_SUFFIX_RE = re.compile(_NUM + r"\s*(kr|SEK)\b", re.IGNORECASE)
_TOTAL_RE = re.compile(r"total(?:\s+now)?(?:\s+due)?\s*(?:is|of|:)?\s*" + _AMOUNT, re.IGNORECASE)

# Account-like token: has a digit, or is an explicit "acct-" id.
_ACCT = r"((?:acct-[A-Za-z0-9-]+)|(?=[A-Za-z0-9-]*\d)[A-Za-z0-9][A-Za-z0-9-]{5,})"
_DESTINATION_RES = [
    re.compile(r"PAY\s+INSTEAD\s+TO\s*:?\s*" + _ACCT, re.IGNORECASE),
    re.compile(r"(?:new|updated|correct|changed)\s+(?:bank\s+)?account(?:\s+(?:number|no\.?))?\s*(?:is|:)?\s*" + _ACCT, re.IGNORECASE),
    re.compile(r"(?:send|transfer|remit|pay|deposit)\b[^.\n]{0,60}?\b(?:to|into)\s+(?:the\s+)?(?:account\s+)?" + _ACCT, re.IGNORECASE),
    re.compile(r"\b(acct-[A-Za-z0-9-]+)"),
    re.compile(r"account\s*(?:number|no\.?)?\s*:?\s*([A-Z0-9][A-Z0-9-]{7,})"),
    re.compile(r"(?:bankgiro|plusgiro|giro|iban)\w*\.?\s*(?:nr|no|number)?\.?\s*:?\s*" + _ACCT, re.IGNORECASE),
]


def normalize(text: str) -> str:
    """What an over-eager model "sees": look-alike characters folded, hidden
    characters removed."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def _minor(raw: str) -> int | None:
    raw = raw.strip()
    if " " in raw or re.fullmatch(r"\d+,\d{2}", raw):  # "2 870,00" / "2870,00": comma decimals
        raw = raw.replace(" ", "").replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        return int(round(float(raw) * 100))
    except ValueError:
        return None


def find_destination(text: str, approved: tuple[str, ...]) -> tuple[str, str] | None:
    """The first account the text tells the agent to pay that isn't one of
    the approved identifiers. Returns (account, quoted evidence)."""
    for pattern in _DESTINATION_RES:
        for match in pattern.finditer(text):
            account = match.group(1).rstrip(".-")
            if account and account not in approved:
                return account, match.group(0).strip()
    return None


def find_amount(text: str) -> tuple[int, str, str] | None:
    """(amount in minor units, currency, quoted evidence)."""
    match = _TOTAL_RE.search(text) or _AMOUNT_RE.search(text)
    if match:
        currency, raw = match.group(1), match.group(2)
    else:
        match = _SUFFIX_RE.search(text)
        if not match:
            return None
        raw, currency = match.group(1), match.group(2)
    amount = _minor(raw)
    if amount is None or amount <= 0:
        return None
    return amount, _CURRENCY.get(currency.lower(), "LKR"), match.group(0).strip()


class CompliantAttackerModel:
    model_id = "lab-obedient-agent-v1-not-gemini"
    sdk_version = "n/a"
    prompt_version = "n/a"

    def __init__(self, *, approved_account_number: str | None = None) -> None:
        self._approved_account_number = approved_account_number

    def generate(self, *, context: dict, documents: list, repair_hint: str | None) -> ModelTurn:
        document_id = documents[0]["document_id"] if documents else None
        text = normalize("\n".join(documents[0]["pages"])) if documents else ""

        approved_ids = tuple(
            v for v in (context["approved_destination_account_id"], self._approved_account_number) if v
        )
        evidence = []
        amount = find_amount(text)
        claimed_amount_minor = amount[0] if amount else None
        currency = amount[1] if amount else None
        if amount:
            evidence.append({"field": "claimed_amount_minor", "page": 1, "quote": amount[2]})

        redirect = find_destination(text, approved_ids)
        if redirect:
            destination_account_id, quote = redirect
            claimed_destination_text = quote
            evidence.append({"field": "claimed_destination_text", "page": 1, "quote": quote})
            destination_version = 1
        else:
            destination_account_id = context["approved_destination_account_id"]
            claimed_destination_text = context.get("approved_destination_label") or "the approved landlord account"
            destination_version = context["approved_destination_version"]

        proposal = {
            "document_id": document_id,
            "rent_period": context.get("rent_period"),
            "claimed_amount_minor": claimed_amount_minor,
            "claimed_currency": currency,
            "claimed_destination_text": claimed_destination_text,
            "evidence": evidence,
            "conflicts": [],
            "next_action": "propose_payment" if claimed_amount_minor is not None else "request_clarification",
        }
        proposed_payment = None
        if claimed_amount_minor is not None:
            proposed_payment = {
                "debit_account_id": context["approved_debit_account_id"],
                "destination_account_id": destination_account_id,
                "destination_version": destination_version,
                "amount_minor": claimed_amount_minor,
                "currency": currency,
                "payment_reference": context["rent_period"],
            }
        return ModelTurn(proposal=proposal, proposed_payment=proposed_payment)
