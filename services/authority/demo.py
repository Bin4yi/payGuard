"""Fresh-browser demo scenario creation and isolated seeding.

SPEC.md Section 14 "Fresh-browser demo access and interface" and Section 12
(`POST /demo/scenarios`, `POST /internal/demo/scenarios/seed`).

Every scenario the server creates is a brand-new, server-assigned id with
its own tenant/landlord accounts, destination and obligation — "reset" never
mutates an existing scenario, so a prior in-flight or paid obligation is
never touched, let alone un-paid. The owner's "current scenario" is just a
pointer that moves forward; nothing behind it is rewritten.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from services.shared.states import ObligationState
from services.shared.store import Store

DEMO_SCENARIOS = "demo_scenarios"
DEMO_OWNER_POINTERS = "demo_owner_pointers"
ACCOUNTS = "accounts"
DESTINATIONS = "destinations"
RENT_INSTRUCTIONS = "rent_instructions"
RENT_INSTRUCTION_VERSIONS = "rent_instruction_versions"
OBLIGATIONS = "obligations"

DEFAULT_TENANT_BALANCE_MINOR = 50_000_000
DEFAULT_AMOUNT_MINOR = 5_000_000
CURRENCY = "LKR"
MAX_SCENARIOS_PER_OWNER = 10  # aggregate per-session quota (SPEC.md Section 14)


class DemoError(Exception):
    pass


class DemoQuotaExceeded(DemoError):
    pass


@dataclass(frozen=True)
class ScenarioHandles:
    scenario_id: str
    obligation_id: str
    instruction_id: str
    debit_account_id: str
    destination_account_id: str
    destination_version: int
    demo_account_id: str
    amount_minor: int
    currency: str
    payment_reference: str


def handles_for(scenario_id: str) -> ScenarioHandles:
    return ScenarioHandles(
        scenario_id=scenario_id,
        obligation_id=f"rent-{scenario_id}",
        instruction_id=f"instruction-{scenario_id}",
        debit_account_id=f"acct-tenant-{scenario_id}",
        destination_account_id=f"acct-landlord-{scenario_id}",
        destination_version=1,
        demo_account_id=f"demo-landlord-account-{scenario_id}",
        amount_minor=DEFAULT_AMOUNT_MINOR,
        currency=CURRENCY,
        payment_reference=f"RENT-{scenario_id}",
    )


def create_or_reset_scenario(store: Store, *, owner_uid: str, now_fn=time.time) -> ScenarioHandles:
    """`POST /demo/scenarios`: create a fresh, isolated scenario for this
    owner. Calling it again ("reset") never mutates the previous scenario —
    it only creates a new one and moves the owner's "current" pointer
    forward. Old paid/in-flight obligations are left exactly as they were.
    """
    existing_count = sum(
        1
        for doc_id in store.list_ids(DEMO_SCENARIOS)
        if (store.peek(DEMO_SCENARIOS, doc_id) or {}).get("owner_uid") == owner_uid
    )
    if existing_count >= MAX_SCENARIOS_PER_OWNER:
        raise DemoQuotaExceeded(f"owner {owner_uid!r} has reached the {MAX_SCENARIOS_PER_OWNER}-scenario demo quota")

    scenario_id = secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:12]
    handles = handles_for(scenario_id)

    def txn(t):
        t.set(
            ACCOUNTS,
            handles.debit_account_id,
            {"account_id": handles.debit_account_id, "owner_uid": owner_uid, "currency": handles.currency, "balance_minor": 0},
        )
        t.set(
            ACCOUNTS,
            handles.demo_account_id,
            {"account_id": handles.demo_account_id, "owner_uid": f"landlord-{scenario_id}", "currency": handles.currency, "balance_minor": 0},
        )
        t.set(
            DESTINATIONS,
            f"{handles.destination_account_id}:{handles.destination_version}",
            {
                "destination_account_id": handles.destination_account_id,
                "version": handles.destination_version,
                "owner_uid": owner_uid,
                "demo_account_id": handles.demo_account_id,
                "account_number": "DEMO-LANDLORD-001",
                "label": "Demo Landlord",
            },
        )
        instruction = {
            "instruction_id": handles.instruction_id,
            "owner_uid": owner_uid,
            "version": 1,
            "destination_account_id": handles.destination_account_id,
            "destination_version": handles.destination_version,
            "amount_minor": handles.amount_minor,
            "currency": handles.currency,
        }
        t.set(RENT_INSTRUCTIONS, handles.instruction_id, instruction)
        t.set(RENT_INSTRUCTION_VERSIONS, f"{handles.instruction_id}:1", instruction)
        t.set(
            OBLIGATIONS,
            handles.obligation_id,
            {
                "obligation_id": handles.obligation_id,
                "owner_uid": owner_uid,
                "state": ObligationState.UNPAID.value,
                "payment_id": None,
                "current_mandate_id": None,
                "current_grant_id": None,
            },
        )
        t.set(
            DEMO_SCENARIOS,
            scenario_id,
            {
                "scenario_id": scenario_id,
                "owner_uid": owner_uid,
                "obligation_id": handles.obligation_id,
                "created_at": int(now_fn()),
                "seeded": False,
            },
        )
        t.set(DEMO_OWNER_POINTERS, owner_uid, {"owner_uid": owner_uid, "current_scenario_id": scenario_id})
        return handles

    result = store.run_transaction(txn)
    seed_scenario_balances(store, scenario_id=scenario_id, owner_uid=owner_uid)
    return result


def seed_scenario_balances(store: Store, *, scenario_id: str, owner_uid: str) -> dict:
    """`POST /internal/demo/scenarios/seed`: authority-only, idempotent.
    Repeated seeding of the same scenario returns its existing result and
    cannot replenish balances — the gateway remains the only runtime balance
    writer once a scenario is live.
    """
    handles = handles_for(scenario_id)

    def txn(t):
        scenario = t.get(DEMO_SCENARIOS, scenario_id)
        if scenario is None or scenario["owner_uid"] != owner_uid:
            raise DemoError("scenario not found or not owned")
        if scenario["seeded"]:
            return scenario  # idempotent — never replenishes balances

        debit = t.get(ACCOUNTS, handles.debit_account_id)
        if debit is None:
            raise DemoError("scenario accounts missing")
        debit = dict(debit)
        debit["balance_minor"] = DEFAULT_TENANT_BALANCE_MINOR
        t.set(ACCOUNTS, handles.debit_account_id, debit)

        scenario = dict(scenario)
        scenario["seeded"] = True
        t.set(DEMO_SCENARIOS, scenario_id, scenario)
        return scenario

    return store.run_transaction(txn)


def get_current_scenario(store: Store, owner_uid: str) -> ScenarioHandles | None:
    pointer = store.peek(DEMO_OWNER_POINTERS, owner_uid)
    if pointer is None:
        return None
    return handles_for(pointer["current_scenario_id"])
