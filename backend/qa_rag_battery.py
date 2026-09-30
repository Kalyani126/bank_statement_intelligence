"""Live RAG question battery - asks many questions and grades every answer.

Runs against a running backend (default http://127.0.0.1:8000), recomputes
ground truth from /api/v1/transactions and then checks each answer against it,
including anti-hallucination invariants (no aggregate may ever exceed the
dataset total).

    venv/Scripts/python qa_rag_battery.py [--base http://127.0.0.1:8000]
"""

from __future__ import annotations

import sys
from collections import defaultdict
from decimal import Decimal

import httpx

BASE = sys.argv[sys.argv.index("--base") + 1] if "--base" in sys.argv else "http://127.0.0.1:8000"
REFUSAL = "I couldn't determine this confidently from the available statement data."


def D(value) -> Decimal:
    return Decimal(str(value)) if value not in (None, "") else Decimal("0")


def login(client: httpx.Client) -> dict:
    response = client.post(
        f"{BASE}/api/v1/auth/login",
        json={"username": "admin", "password": "admin123"},
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def ground_truth(items: list[dict]) -> dict:
    month = defaultdict(lambda: {"n": 0, "d": Decimal(0), "c": Decimal(0)})
    person = defaultdict(lambda: {"d": Decimal(0), "n": 0, "name": ""})
    method = defaultdict(lambda: {"d": Decimal(0), "n": 0})
    for row in items:
        bucket = month[row["transaction_date"][:7]]
        bucket["n"] += 1
        bucket["d"] += D(row["debit_amount"])
        bucket["c"] += D(row["credit_amount"])
        if row["counterparty_code"]:
            entry = person[row["counterparty_code"]]
            entry["d"] += D(row["debit_amount"])
            entry["n"] += 1
            entry["name"] = row["counterparty_name"]
        bucket_method = method[row["payment_method"]]
        bucket_method["n"] += 1
        bucket_method["d"] += D(row["debit_amount"])

    groups = defaultdict(lambda: {"n": 0, "name": ""})
    for row in items:
        key = row["counterparty_code"] or (row["normalized_counterparty_name"] or "unknown")
        groups[key]["n"] += 1
        groups[key]["name"] = row["counterparty_name"] or key

    debits = [row for row in items if row["debit_amount"]]
    may_debits = [row for row in debits if row["transaction_date"][:7] == "2025-05"]
    return {
        "rows": items,
        "debit": sum(D(row["debit_amount"]) for row in items),
        "credit": sum(D(row["credit_amount"]) for row in items),
        "count": len(items),
        "month": month,
        "person": person,
        "method": method,
        "largest": max(debits, key=lambda row: D(row["debit_amount"])),
        "may_largest": max(may_debits, key=lambda row: D(row["debit_amount"]))
        if may_debits
        else None,
        "last": max(items, key=lambda row: row["transaction_date"]),
        "top_person": max(person.items(), key=lambda kv: kv[1]["d"]),
        "groups": groups,
        "top_group": max(groups.items(), key=lambda kv: (kv[1]["n"], kv[0])),
    }


class Battery:
    def __init__(self, client: httpx.Client, headers: dict, gt: dict) -> None:
        self.client = client
        self.headers = headers
        self.gt = gt
        self.results: list[tuple[str, str, str]] = []

    def ask(self, question: str):
        response = self.client.post(
            f"{BASE}/api/v1/rag/query",
            headers=self.headers,
            json={"question": question},
        )
        if response.status_code != 200:
            return None, f"HTTP {response.status_code} {response.text[:120]}"
        return response.json(), None

    def check(self, question: str, verifier) -> None:
        payload, error = self.ask(question)
        if error:
            self.results.append((question, "FAIL", error))
            return

        aggregates = payload.get("aggregates") or {}
        problems = []
        if not (payload.get("answer") or "").strip():
            problems.append("empty answer")
        if aggregates.get("total_debit") and D(aggregates["total_debit"]) > self.gt["debit"]:
            problems.append("total_debit exceeds dataset")
        if aggregates.get("total_credit") and D(aggregates["total_credit"]) > self.gt["credit"]:
            problems.append("total_credit exceeds dataset")
        if aggregates.get("count") is not None and int(aggregates["count"]) > self.gt["count"]:
            problems.append("count exceeds dataset")
        if problems:
            self.results.append((question, "FAIL", "; ".join(problems)))
            return

        try:
            ok, detail = verifier(payload)
        except Exception as error:  # noqa: BLE001 - report, never crash the run
            ok, detail = False, f"checker error: {type(error).__name__}: {error}"
        self.results.append((question, "PASS" if ok else "FAIL", detail))

    # --- verifiers ---------------------------------------------------------
    def totals(self, debit=None, credit=None, count=None):
        def verify(payload):
            aggregates = payload["aggregates"]
            messages = []
            if debit is not None and D(aggregates.get("total_debit")) != debit:
                messages.append(f"debit {aggregates.get('total_debit')} != {debit}")
            if credit is not None and D(aggregates.get("total_credit")) != credit:
                messages.append(f"credit {aggregates.get('total_credit')} != {credit}")
            if count is not None:
                raw_count = aggregates.get("count")
                if raw_count is None or int(raw_count) != count:
                    messages.append(f"count {raw_count} != {count}")
            return not messages, "; ".join(messages) or f"matches GT ({debit}/{credit}/{count})"

        return verify

    def refusal(self, payload):
        honest = payload["answer"].strip() == REFUSAL
        return honest, (
            "honest refusal - nothing invented"
            if honest
            else f"got: {payload['answer'][:70]!r}"
        )

    def ambiguous(self, payload):
        ok = (
            payload["person_resolution"] == "ambiguous"
            and len(payload["person_candidates"]) >= 2
            and not payload["aggregates"]
        )
        return ok, (
            f"resolution={payload['person_resolution']} "
            f"candidates={len(payload['person_candidates'])} agg={payload['aggregates']}"
        )

    def person_sum(self, payload, month_key=None, side="debit"):
        if payload["person_resolution"] != "resolved":
            return False, f"resolution={payload['person_resolution']}"
        codes = {item["code"] for item in payload["person_candidates"]}
        expected = Decimal(0)
        expected_count = 0
        for row in self.gt["rows"]:
            if row["counterparty_code"] not in codes:
                continue
            if month_key and row["transaction_date"][:7] != month_key:
                continue
            expected += D(row["debit_amount"] if side == "debit" else row["credit_amount"])
            expected_count += 1
        aggregates = payload["aggregates"]
        field = "total_debit" if side == "debit" else "total_credit"
        got = D(aggregates.get(field))
        raw_count = aggregates.get("count")
        got_count = int(raw_count) if raw_count is not None else -1
        ok = got == expected and got_count == expected_count
        return ok, f"codes={sorted(codes)} expected {expected}/{expected_count} got {got}/{got_count}"

    def person_method_count(self, payload):
        if payload["person_resolution"] != "resolved":
            return False, f"resolution={payload['person_resolution']}"
        widened = "METHOD_FILTER_WIDENED" in payload["notes"]
        family = {"UPI", "PAYTM", "GOOGLE_PAY", "PHONEPE"} if widened else {"UPI"}
        codes = {item["code"] for item in payload["person_candidates"]}
        expected = sum(
            1
            for row in self.gt["rows"]
            if row["counterparty_code"] in codes and row["payment_method"] in family
        )
        got = payload["aggregates"].get("count")
        got = int(got) if got is not None else -1
        return got == expected, f"expected {expected} got {got} widened={widened}"


def main() -> int:
    client = httpx.Client(timeout=60.0)
    headers = login(client)
    items = client.get(f"{BASE}/api/v1/transactions?limit=500", headers=headers).json()["items"]
    if not items:
        print("No transactions found - upload a statement first.")
        return 1

    gt = ground_truth(items)
    print(f"GT: count={gt['count']} debit={gt['debit']} credit={gt['credit']}")
    print(f"GT months: {dict(sorted((k, v['n']) for k, v in gt['month'].items()))}")
    print(f"GT methods: { {k: v['n'] for k, v in gt['method'].items()} }")
    print(f"GT persons: { {k: (v['name'], str(v['d'])) for k, v in gt['person'].items()} }")
    print(
        f"GT largest={gt['largest']['debit_amount']} on {gt['largest']['transaction_date']} | "
        f"last balance={gt['last']['balance']} on {gt['last']['transaction_date']}\n"
    )

    battery = Battery(client, headers, gt)
    month = gt["month"]
    method = gt["method"]

    battery.check("What is my total credit and debit?", battery.totals(gt["debit"], gt["credit"]))
    battery.check("How much did I spend in total?", battery.totals(debit=gt["debit"]))
    battery.check("How much did I receive in total?", battery.totals(credit=gt["credit"]))
    battery.check("How many transactions in 2025?", battery.totals(count=gt["count"]))
    battery.check("How many transactions in April 2025?", battery.totals(count=month["2025-04"]["n"]))
    battery.check("How many transactions in May 2025?", battery.totals(count=month["2025-05"]["n"]))
    battery.check("How many transactions in June 2025?", battery.totals(count=month["2025-06"]["n"]))
    battery.check("How many transactions in October 2025?", battery.totals(count=month["2025-10"]["n"]))
    battery.check("How much did I spend in June 2025?", battery.totals(debit=month["2025-06"]["d"]))
    battery.check("How much did I receive in April 2025?", battery.totals(credit=month["2025-04"]["c"]))
    battery.check("How much did I receive in October 2025?", battery.totals(credit=month["2025-10"]["c"]))

    def empty_period(payload):
        raw = payload["aggregates"].get("count")
        empty = raw is not None and int(raw) == 0
        honest = payload["answer"].strip() == REFUSAL
        return empty and honest, f"count={raw} refusal={honest}"

    battery.check("How many transactions in 2024?", empty_period)
    battery.check("How many transactions last month?", empty_period)

    def bounded_range(payload):
        aggregates = payload["aggregates"]
        ok = (
            int(aggregates.get("count") or -1) == month["2025-05"]["n"]
            and aggregates.get("first_date", "") >= "2025-05-01"
            and aggregates.get("last_date", "") <= "2025-05-31"
        )
        return ok, (
            f"count={aggregates.get('count')} "
            f"range={aggregates.get('first_date')}..{aggregates.get('last_date')}"
        )

    battery.check("Show all transactions in May 2025", bounded_range)
    battery.check("How much did I send Ravi Kumar?", lambda p: battery.person_sum(p))
    battery.check("How much did I send Ravi?", battery.ambiguous)
    battery.check("How much did I send to Gandhi ji?", battery.refusal)
    battery.check("How many transactions with Ramesh?", lambda p: battery.person_sum(p))
    battery.check(
        "How much did I send Kalyan Kumar in April 2025?",
        lambda p: battery.person_sum(p, month_key="2025-04"),
    )
    battery.check("Show all UPI transactions with Kalyan", battery.person_method_count)
    battery.check("How many UPI transactions did I make?", battery.totals(count=method["UPI"]["n"]))
    battery.check("Show all ATM transactions", battery.totals(count=method["ATM"]["n"]))
    battery.check("How much did I pay via NEFT?", battery.totals(debit=method["NEFT"]["d"]))
    battery.check("How much did I pay via Paytm?", battery.totals(debit=method["PAYTM"]["d"]))

    battery.check(
        "Show my biggest transactions",
        lambda p: (
            D(p["aggregates"].get("largest_debit")) == D(gt["largest"]["debit_amount"]),
            f"expected {gt['largest']['debit_amount']} got {p['aggregates'].get('largest_debit')}",
        ),
    )
    battery.check(
        "What was my biggest transaction in May 2025?",
        lambda p: (
            gt["may_largest"] is not None
            and D(p["aggregates"].get("largest_debit")) == D(gt["may_largest"]["debit_amount"]),
            f"expected {gt['may_largest']['debit_amount'] if gt['may_largest'] else '-'} "
            f"got {p['aggregates'].get('largest_debit')}",
        ),
    )

    top_code, top = gt["top_person"]
    battery.check(
        "Who did I transfer the most money to?",
        lambda p: (
            ((p["aggregates"].get("counterparties") or {}).get("top") or [{}])[0].get("total_debit")
            == str(top["d"])
            and top_code in p["answer"],
            f"expected {top['name']} {top['d']}; answer={p['answer'][:70]!r}",
        ),
    )
    def counterparties(payload):
        breakdown = payload.get("aggregates", {}).get("counterparties") or {}
        expected_distinct = len(gt["groups"])
        ok = (
            payload["intent"] == "counterparties"
            and int(breakdown.get("distinct") or 0) == expected_distinct
            and (breakdown.get("top") or [{}])[0].get("name") == gt["top_group"][1]["name"]
        )
        return ok, (
 f"distinct={breakdown.get('distinct')} expected={expected_distinct}; "
            f"top={((breakdown.get('top') or [{}])[0].get('name'))!r} "
            f"expected={gt['top_group'][1]['name']!r} ({gt['top_group'][1]['n']} txns)"
        )

    battery.check("Who did I transact with?", counterparties)
    battery.check(
        "What is my balance?",
        lambda p: (
            D(p["aggregates"].get("closing_balance")) == D(gt["last"]["balance"])
            and p["aggregates"].get("balance_date") == gt["last"]["transaction_date"],
            f"expected {gt['last']['balance']}@{gt['last']['transaction_date']} "
            f"got {p['aggregates'].get('closing_balance')}@{p['aggregates'].get('balance_date')}",
        ),
    )

    battery.check(
        "What did I spend on food?",
        lambda p: (
            p["answer"].strip() == REFUSAL or not p["aggregates"],
            f"answer={p['answer'][:70]!r} agg={p['aggregates']}",
        ),
    )
    battery.check("Tell me about my dog", battery.refusal)
    battery.check("What is my passport number?", battery.refusal)
    battery.check("Who is the president of France?", battery.refusal)

    width = max(len(question) for question, _, _ in battery.results)
    print("=" * (width + 70))
    for question, status, detail in battery.results:
        print(f"[{status}] {question:<{width}}  {detail}")
    print("=" * (width + 70))
    passed = sum(1 for _, status, _ in battery.results if status == "PASS")
    print(f"TOTAL: {passed}/{len(battery.results)} passed")
    return 0 if passed == len(battery.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
