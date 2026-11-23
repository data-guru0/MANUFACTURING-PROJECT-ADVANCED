"""The customers, simulated over real email: they read what Apex sends them and answer.

Every customers.local mailbox is polled; each unread email from apex.local (a quote an estimator
sent from Roundcube, a question...) gets one outcome, chosen from its Message-ID so the same email
always gets the same answer:
  accept 25 % | reject with a reason 30 % | negotiate 15 % | ask a question 10 % | silence 20 %.
In class a human can answer by hand in Roundcube instead (log in as the customer).

Runs as the `customers` container (see docker-compose.legacy.yml); `--once` polls one round.
ponytail: replies are English and immediate; real customers take days. Add a delay queue if a
phase needs realistic reply times.
"""

import argparse
import hashlib
import os
import random
from email.message import Message

from legacy.mail.client import OTHERS, handle_unseen_from_apex, mailboxes, poll_forever, reply
from legacy.seed.history import LOST_REASONS, generate

OUTCOMES = {"accept": 25, "reject": 30, "negotiate": 15, "question": 10, "silence": 20}
QUESTIONS = [
    "Does the price include anodizing, or is that extra?",
    "Can you deliver in 2 weeks instead? What would that cost?",
    "Is a 3.1 material certificate included in the price?",
    "What is the price for double the quantity?",
    "Can you confirm the tolerance on the bore is achievable?",
]


def outcome_for(message_id: str) -> str:
    rng = random.Random(hashlib.sha256(message_id.encode()).hexdigest())
    return rng.choices(list(OUTCOMES), weights=list(OUTCOMES.values()))[0]


def answer(outcome: str, message_id: str) -> str:
    rng = random.Random(message_id)
    if outcome == "accept":
        return (f"Thank you, we accept your quote. Our purchase order PO-{rng.randint(10000, 99999)} "
                "follows by separate mail.")  # fmt: skip
    if outcome == "reject":
        reason = rng.choice([r for r in LOST_REASONS if r != "We replied too late"] + ["Your reply came too late"])
        return f"Thank you for the quote. Unfortunately we will not place the order.\nReason: {reason}."
    if outcome == "negotiate":
        return (f"Thanks. Your price is about {rng.choice([8, 10, 12, 15])} % above another offer we have. "
                "Can you improve it?")  # fmt: skip
    return rng.choice(QUESTIONS)


def customer_boxes() -> dict[str, str]:
    """mailbox -> company name (for the From display name)."""
    from legacy.mail.client import prospect_email
    from legacy.seed.rfq_parts import PROSPECTS

    h = generate()
    names = {c.email: c.name for c in h.customers if c.duplicate_of is None}
    names |= {c.email: c.name for c in h.customers if c.email not in names}
    names |= {prospect_email(n): n for _, n, *_ in PROSPECTS}
    return {a: names[a] for a in mailboxes(h) if a.endswith("@customers.local") and a not in OTHERS}


def respond_once(boxes: dict[str, str] | None = None) -> int:
    boxes = boxes or customer_boxes()
    done = 0
    for address, company in boxes.items():

        def handle(msg: Message, address: str = address, company: str = company) -> None:
            mid = str(msg.get("Message-ID", ""))
            outcome = outcome_for(mid)
            if outcome != "silence":
                reply(msg, address, company, answer(outcome, mid) + f"\n\nRegards,\n{company}")
            print(f"customers: {company} <- {msg.get('Subject')!s:.60}  => {outcome}")

        done += handle_unseen_from_apex(address, handle)
    return done


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    boxes = customer_boxes()
    if a.once:
        print(f"customers: answered {respond_once(boxes)} emails")
    else:
        poll_forever("customers", lambda: respond_once(boxes), float(os.environ.get("RESPONDER_INTERVAL", "10")))


if __name__ == "__main__":
    main()
