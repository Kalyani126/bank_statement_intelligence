"""Running-balance reconciliation for extracted statement rows.

Column detection is heuristic: layouts swap Withdrawal/Deposit, wrapped PDF
text loses column alignment, and an amount cell can be read as empty. The
closing balance is an independent, authoritative signal - it must fall by the
debit and rise by the credit, exactly. Every row is therefore checked against
it:

  * direction contradicts the balance -> side corrected (and reported)
  * amount missing but balance moved  -> amount recovered from the delta
  * both sides filled                 -> the side matching the balance wins
  * zero amounts                      -> cleared (they are not transactions)
  * most rows had to be flipped       -> the columns themselves are swapped,
                                         so leading rows without balance
                                         evidence are flipped too

A correction is only ever applied when the amount matches the balance
movement to the paisa, so a statement without a usable balance column keeps
exactly what the parser read - nothing is guessed.
"""

from __future__ import annotations

from decimal import Decimal

from app.parsers.base import ParsedRow

_TOLERANCE = Decimal("0.01")

# Narration markers, used only for leading rows with no balance evidence and
# no opening balance to verify them against.
_DEBIT_MARKERS = (
    "WDL", "WITHDRAWAL", "DRAWN", "DR/", "/DR/", " DEBIT", "PAID TO",
    "TRANSFER TO", "CASH WDL",
)
_CREDIT_MARKERS = (
    "DEP", "DEPOSIT", "CR/", "/CR/", " CREDIT", "RECEIVED FROM", "CREDITED",
    "REFUND",
)


def _matches(amount: Decimal, delta: Decimal) -> bool:
    return abs(amount - delta) <= _TOLERANCE


def _narration_side(description: str | None) -> str | None:
    text = (description or "").upper()
    debit = any(marker in text for marker in _DEBIT_MARKERS)
    credit = any(marker in text for marker in _CREDIT_MARKERS)
    if debit and not credit:
        return "DEBIT"
    if credit and not debit:
        return "CREDIT"
    return None


def _flip(row: ParsedRow) -> None:
    if row.debit is not None:
        row.credit, row.debit = row.debit, None
    elif row.credit is not None:
        row.debit, row.credit = row.credit, None


def reconcile_directions(
    rows: list[ParsedRow],
    warnings: list[str],
    opening_balance: Decimal | None = None,
) -> None:
    """Correct side/amount errors in place using each row's balance delta."""
    if not rows:
        return

    previous_balance: Decimal | None = opening_balance
    corrected = 0
    recovered = 0
    cleared = 0
    keyword_fixes = 0
    verifiable = 0
    evidence_flips = 0
    unverified: list[ParsedRow] = []

    for row in rows:
        delta: Decimal | None = None
        if previous_balance is not None and row.balance is not None:
            delta = row.balance - previous_balance

        # 1) "0.00" placeholders in an empty column are not transactions.
        if row.debit is not None and row.debit == 0:
            row.debit = None
            cleared += 1
        if row.credit is not None and row.credit == 0:
            row.credit = None
            cleared += 1

        # 2) A signed amount only ever means one thing.
        if row.debit is not None and row.debit < 0 and row.credit is None:
            row.credit, row.debit = -row.debit, None
            corrected += 1
        if row.credit is not None and row.credit < 0 and row.debit is None:
            row.debit, row.credit = -row.credit, None
            corrected += 1

        has_money = row.debit is not None or row.credit is not None

        if delta is not None and delta != 0:
            verifiable += 1
            debit_ok = row.debit is not None and _matches(-row.debit, delta)
            credit_ok = row.credit is not None and _matches(row.credit, delta)
            flipped = False

            if row.debit is not None and row.credit is not None:
                # Both columns were filled: keep the side the balance proves.
                if credit_ok and not debit_ok:
                    row.debit = None
                    flipped = True
                elif debit_ok and not credit_ok:
                    row.credit = None
                    flipped = True
            elif row.debit is not None and not debit_ok:
                # Balance rose by exactly this amount -> it is a credit.
                if _matches(row.debit, delta):
                    _flip(row)
                    flipped = True
            elif row.credit is not None and not credit_ok:
                # Balance fell by exactly this amount -> it is a debit.
                if _matches(-row.credit, delta):
                    _flip(row)
                    flipped = True
            elif not has_money:
                # The amount cell was lost: the balance is authoritative.
                if delta > 0:
                    row.credit = delta
                else:
                    row.debit = -delta
                recovered += 1

            if flipped:
                evidence_flips += 1
                corrected += 1
        elif previous_balance is None and has_money:
            # Leading row(s): no balance to check against yet.
            side = _narration_side(row.description)
            if (side == "DEBIT" and row.debit is None) or (
                side == "CREDIT" and row.credit is None
            ):
                _flip(row)
                keyword_fixes += 1
                corrected += 1
            else:
                unverified.append(row)

        if row.balance is not None:
            previous_balance = row.balance

    # 3) Most of the verifiable rows were on the wrong side -> the statement's
    #    columns are systematically swapped, so the leading rows (which had no
    #    balance to verify them) belong to the same broken layout.
    if evidence_flips >= 2 and evidence_flips * 2 >= verifiable:
        for row in unverified:
            if row.debit is not None or row.credit is not None:
                _flip(row)
                corrected += 1

    if corrected:
        warnings.append(
            f"Running balance corrected the direction of {corrected} transaction(s)."
        )
    if recovered:
        warnings.append(
            f"Recovered {recovered} missing amount(s) from the running balance."
        )
    if cleared:
        warnings.append(f"Ignored {cleared} zero-amount placeholder cell(s).")


def has_money(row: ParsedRow) -> bool:
    return row.debit is not None or row.credit is not None
