from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, timedelta
import csv
import io
import math
import random
import re

import pandas as pd
import streamlit as st


# ----------------------------- Settings ---------------------------------

MIN_DEBIT_CENTS = 500     # smallest allowed debit: 5.00
MIN_CREDIT_CENTS = 5000   # smallest allowed credit: 50.00

BIG_THRESHOLD_CENTS = 200000          # "above 2,000.00"
BIG_MIN_CENTS = 220000                # smallest "big" entry: 2,200.00,
                                      # so none sits right at 2,000.01

# Typical ranges (in cents) used to make amounts look natural.
NORMAL_DEBIT_RANGE = (1500, 150000)       # 15.00 - 1,500.00
NORMAL_CREDIT_RANGE = (10000, 180000)     # 100.00 - 1,800.00
BIG_RANGE = (250000, 2000000)             # 2,500.00 - 20,000.00

PROJECT_HEADER = "SCHOOL PROJECT"
INDEX_NUMBER = "ED/13/0029"

DATE_FORMAT = "%d-%b-%Y"              # e.g. 28-Feb-2026

COLUMNS = [
    "Trans. Date", "Value. Date", "Debits",
    "Credits", "Balance", "Remarks"
]

DEBIT_REMARKS = [
    "SAMPLE CASH WITHDRAWAL JOHN DOE TOWN A",
    "SAMPLE CASH WITHDRAWAL FROM ATM 000001 CITY X",
    "SAMPLE CHARGES /// MONTH YEAR CARD MONTHLY FEES",
    "SAMPLE POS TRANSACTION FEE EXAMPLE-SERVICE.COM 000000 USD",
    "SAMPLE ONLINE PURCHASE EXAMPLE STORE",
    "SAMPLE TRANSFER OUT JANE ROE",
]

CREDIT_REMARKS = [
    "SAMPLE CASH DEPOSIT JOHN DOE TOWN A",
    "SAMPLE TRANSFER IN JANE ROE",
    "SAMPLE REFUND EXAMPLE STORE",
    "SAMPLE INTEREST CREDIT",
]

# Weekend rule: on Saturdays and Sundays only ATM transactions are possible.
# A remark is an ATM transaction when it contains the word "ATM", so any
# remark added above with "ATM" in it may also fall on a weekend.

# An ATM never pays out cents: every ATM withdrawal is a whole number such
# as 20.00, 48.00 or 51.00, never 20.83. The value is the step in cents
# (100 = whole numbers; 1000 would allow only multiples of 10.00).
ATM_NOTE_CENTS = 100

ESTIMATED_TAG = "(ESTIMATED / NOT BANK-SOURCED)"
KNOWN_DEFAULT_REMARK = "KNOWN TRANSACTION (UPLOADED)"
SALARY_REMARK = "SAMPLE SALARY ENTRY"
DATE_RETRIES = 100          # tries at keeping the balance above zero

KNOWN_DATE_COLUMNS = ["date", "trans. date", "trans_date", "transaction date"]
KNOWN_REMARK_COLUMNS = [
    "remarks", "remark", "details", "description", "narration"
]
KNOWN_DEBIT_COLUMNS = ["debits", "debit", "withdrawals", "withdrawal"]
KNOWN_CREDIT_COLUMNS = ["credits", "credit", "deposits", "deposit"]

# Rows starting with these words are footers/summaries, not transactions.
NON_TRANSACTION_PREFIXES = (
    "total", "uncleared", "opening", "closing", "balance"
)


# --------------------------- Money helpers -------------------------------

def to_cents(value) -> int:
    """Convert a dollar value (e.g. '1,000.50') to integer cents."""
    try:
        text = str(value).replace(",", "").replace("$", "").strip()
        amount = Decimal(text).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )
        return int(amount * 100)
    except InvalidOperation:
        raise ValueError(f"Invalid monetary value: {value}")


def from_cents(cents: int) -> str:
    """Format integer cents with thousands separators, e.g. 1,000.00."""
    return f"{Decimal(cents) / Decimal(100):,.2f}"


def money(cents: int) -> str:
    """Format cents as dollars with the sign first: -$825.50."""
    sign = "-" if cents < 0 else ""
    return f"{sign}${from_cents(abs(cents))}"


def plural(count: int, word: str) -> str:
    """'1 Debit', '2 Debits'."""
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


# ---------------------------- Weekend rule -------------------------------

def is_weekend(day: date) -> bool:
    """Saturday or Sunday."""
    return day.weekday() >= 5


def allowed_on_weekend(remark: str) -> bool:
    """Only ATM transactions can be done on a Saturday or Sunday."""
    return re.search(r"\bATM\b", remark, re.IGNORECASE) is not None


def gap_days(start_date: date, end_date: date) -> tuple:
    """(every day, working days only) from start_date to end_date."""
    every_day = [
        start_date + timedelta(days=n)
        for n in range((end_date - start_date).days + 1)
    ]
    return every_day, [day for day in every_day if not is_weekend(day)]


def entry_date(
    rng: random.Random, remark: str, every_day: list, working_days: list
) -> date:
    """A random date for an entry: any day for ATM, else Monday-Friday."""
    return rng.choice(
        every_day if allowed_on_weekend(remark) else working_days
    )


def round_atm_withdrawals(
    amounts: list, remarks: list, rng: random.Random
) -> tuple:
    """
    Make every ATM withdrawal a whole amount with no cents (a multiple of
    ATM_NOTE_CENTS). The cents taken off or added are moved to the largest
    other debit that can take them, so the total of the debits, the number
    of entries and the entries above 2,000.00 all stay the same.

    When no other debit can take the difference (for example the ATM
    withdrawal is the only debit), the ATM remarks are swapped for other
    debit remarks instead, so the amounts stay as they are.

    `amounts` are signed cents (debits negative). Returns new
    (amounts, remarks) lists.
    """
    amounts, remarks = list(amounts), list(remarks)
    atm = [
        k for k, (amount, remark) in enumerate(zip(amounts, remarks))
        if amount < 0 and allowed_on_weekend(remark)
    ]
    if not atm:
        return amounts, remarks

    def still_fits(before: int, after: int) -> bool:
        """Still a valid debit, on the same side of 2,000.00."""
        return after >= MIN_DEBIT_CENTS and (
            (before > BIG_THRESHOLD_CENTS) == (after > BIG_THRESHOLD_CENTS)
        )

    # Round each withdrawal to the nearest whole amount, carrying what was
    # cut off into the next one so the leftover stays small.
    notes = {}
    carry = 0
    for k in atm:
        wanted = -amounts[k] + carry
        notes[k] = max(
            ATM_NOTE_CENTS,
            (wanted + ATM_NOTE_CENTS // 2) // ATM_NOTE_CENTS * ATM_NOTE_CENTS,
        )
        carry = wanted - notes[k]

    # The leftover goes to the largest non-ATM debit that can take it.
    others = sorted(
        (k for k, amount in enumerate(amounts) if amount < 0 and k not in atm),
        key=lambda k: amounts[k],
    )
    taker = next(
        (k for k in others if still_fits(-amounts[k], -amounts[k] + carry)),
        None,
    )

    if (carry == 0 or taker is not None) and all(
        still_fits(-amounts[k], notes[k]) for k in atm
    ):
        for k in atm:
            amounts[k] = -notes[k]
        if carry != 0:
            amounts[taker] -= carry
        return amounts, remarks

    other_remarks = [r for r in DEBIT_REMARKS if not allowed_on_weekend(r)]
    if other_remarks:
        for k in atm:
            remarks[k] = rng.choice(other_remarks)
    return amounts, remarks


# ------------------------- Amount generation -----------------------------

def log_uniform(rng: random.Random, low: int, high: int) -> int:
    """A random amount that is equally likely at every order of magnitude."""
    return int(math.exp(rng.uniform(math.log(low), math.log(high))))


def natural_side_total(
    count: int, big: int, normal_range: tuple, rng: random.Random
) -> int:
    """Total of `count` realistic amounts, `big` of them above 2,000."""
    total = sum(log_uniform(rng, *BIG_RANGE) for _ in range(big))
    total += sum(
        log_uniform(rng, *normal_range) for _ in range(count - big)
    )
    return total


def split_total(
    total: int, parts: int, rng: random.Random, minimum: int = 1
) -> list:
    """
    Split a positive integer total into `parts` integers that add up to
    exactly `total`, with a natural spread of sizes. Every part is at least
    `minimum`, and also at least a tenth of the average, so entries do not
    pile up at the minimum.
    """
    if parts == 1:
        return [total]

    if total < parts * minimum:
        raise ValueError("Total is too small for the requested entries.")

    floor = max(minimum, total // (parts * 10))
    spare = total - parts * floor

    # Integer weights keep the split exact (no float rounding).
    weights = [int(rng.lognormvariate(0, 1.0) * 1_000_000) + 1
               for _ in range(parts)]
    weight_sum = sum(weights)

    amounts = [floor + (spare * w) // weight_sum for w in weights]

    # Floor division can leave a small remainder; give it to the largest.
    amounts[amounts.index(max(amounts))] += total - sum(amounts)
    return amounts


def split_with_big(
    total: int,
    parts: int,
    big_count: int,
    rng: random.Random,
    minimum: int,
) -> list:
    """
    Split `total` into `parts` amounts where exactly `big_count` entries are
    above 2,000.00 (the first `big_count` returned) and the rest are
    between `minimum` and 2,000.00.
    """
    if big_count <= 0:
        return split_total(total, parts, rng, minimum)

    normal_count = parts - big_count
    normal_amounts = []
    normal_total = 0

    if normal_count > 0:
        low = normal_count * minimum
        high = min(
            normal_count * BIG_THRESHOLD_CENTS,
            total - big_count * BIG_MIN_CENTS,
        )
        # Aim for a typical entry of roughly 200.00 - 800.00.
        target = normal_count * rng.randint(20000, 80000)
        normal_total = max(low, min(target, high))
        normal_amounts = split_total(
            normal_total, normal_count, rng, minimum
        )

        # Keep the normal entries at or below the threshold; any excess
        # moves to the big entries.
        for i, amount in enumerate(normal_amounts):
            if amount > BIG_THRESHOLD_CENTS:
                normal_total -= amount - BIG_THRESHOLD_CENTS
                normal_amounts[i] = BIG_THRESHOLD_CENTS

    big_amounts = split_total(
        total - normal_total, big_count, rng, BIG_MIN_CENTS
    )
    return big_amounts + normal_amounts


def tidy_amounts(amounts: list, big_count: int, rng: random.Random) -> list:
    """
    Make amounts look like real money: most are whole numbers (e.g. 350.00)
    while some keep cents. Any difference is absorbed by the largest big
    entry, so the total is unchanged and every limit still holds.
    """
    if len(amounts) == 1:
        return amounts

    total = sum(amounts)
    tidy = [
        (a + 50) // 100 * 100 if rng.random() < 0.6 else a
        for a in amounts
    ]

    # Big entries come first in the list.
    pool = range(big_count) if big_count > 0 else range(len(tidy))
    largest = max(pool, key=lambda i: tidy[i])
    tidy[largest] += total - sum(tidy)
    return tidy


def generate_items(
    net_cents: int,
    debit_count: int,
    credit_count: int,
    start_date: date,
    end_date: date,
    seed: int = 1,
) -> list:
    """
    Generate synthetic entries with exactly `debit_count` debits and
    `credit_count` credits, where credits - debits equals net_cents.

    Each item is a dict: date, amount (cents; credits positive, debits
    negative), remark and source.
    """
    if debit_count < 0 or credit_count < 0:
        raise ValueError("Entry counts cannot be negative.")

    if end_date < start_date:
        raise ValueError("End date cannot be before start date.")

    if net_cents == 0:
        return []

    if debit_count + credit_count < 1:
        raise ValueError("Enter at least one debit or credit.")

    every_day, working_days = gap_days(start_date, end_date)
    if not working_days:
        raise ValueError(
            "The gap dates fall entirely on a weekend, when only ATM "
            "transactions are possible. Choose dates that include a "
            "working day (Monday to Friday)."
        )

    magnitude = abs(net_cents)
    rng = random.Random(seed)

    if net_cents < 0 and debit_count < 1:
        raise ValueError(
            f"The missing amount is a net debit of "
            f"{from_cents(magnitude)}, so at least 1 debit is needed."
        )
    if net_cents > 0 and credit_count < 1:
        raise ValueError(
            f"The missing amount is a net credit of "
            f"{from_cents(magnitude)}, so at least 1 credit is needed."
        )

    # How many entries must be above 2,000.00:
    # debits: a random 2 to 5, credits: either 2 or 4 (capped by the counts).
    big_debits = min(rng.randint(2, 5), debit_count)
    big_credits = min(rng.choice([2, 4]), credit_count)

    # The "larger" side (debits for a net debit, credits for a net credit)
    # exceeds the "smaller" side by exactly the missing amount.
    if net_cents < 0:
        large_count, large_min, large_big, large_range = (
            debit_count, MIN_DEBIT_CENTS, big_debits, NORMAL_DEBIT_RANGE
        )
        small_count, small_min, small_big, small_range = (
            credit_count, MIN_CREDIT_CENTS, big_credits, NORMAL_CREDIT_RANGE
        )
    else:
        large_count, large_min, large_big, large_range = (
            credit_count, MIN_CREDIT_CENTS, big_credits, NORMAL_CREDIT_RANGE
        )
        small_count, small_min, small_big, small_range = (
            debit_count, MIN_DEBIT_CENTS, big_debits, NORMAL_DEBIT_RANGE
        )

    def needed(count, minimum, big):
        return big * BIG_MIN_CENTS + (count - big) * minimum

    # With no smaller side the larger side must equal the missing amount,
    # so lower the number of big entries if they cannot fit.
    if small_count == 0:
        while large_big > 0 and needed(
            large_count, large_min, large_big
        ) > magnitude:
            large_big -= 1

    large_needed = needed(large_count, large_min, large_big)
    small_needed = needed(small_count, small_min, small_big)

    if small_count > 0:
        # Make BOTH sides at least as large as a realistic set of amounts
        # would be, and never below their minimums. This keeps entries
        # from collapsing to the smallest allowed values.
        small_natural = natural_side_total(
            small_count, small_big, small_range, rng
        )
        large_natural = natural_side_total(
            large_count, large_big, large_range, rng
        )
        small_total = max(
            small_natural,
            large_natural - magnitude,
            small_needed,
            large_needed - magnitude,
        )
    else:
        small_total = 0

    large_total = small_total + magnitude

    if large_total < large_needed:
        raise ValueError(
            "The amount is too small to split into that many entries "
            "at the minimum sizes. Reduce the number of entries."
        )

    if net_cents < 0:
        debit_total, credit_total = large_total, small_total
        big_debits, big_credits = large_big, small_big
    else:
        credit_total, debit_total = large_total, small_total
        big_credits, big_debits = large_big, small_big

    amounts = []
    if credit_count > 0:
        amounts += tidy_amounts(
            split_with_big(
                credit_total, credit_count, big_credits, rng,
                MIN_CREDIT_CENTS
            ),
            big_credits, rng,
        )
    if debit_count > 0:
        amounts += [
            -c for c in tidy_amounts(
                split_with_big(
                    debit_total, debit_count, big_debits, rng,
                    MIN_DEBIT_CENTS
                ),
                big_debits, rng,
            )
        ]

    rng.shuffle(amounts)

    remarks = [
        rng.choice(DEBIT_REMARKS if amount < 0 else CREDIT_REMARKS)
        for amount in amounts
    ]
    amounts, remarks = round_atm_withdrawals(amounts, remarks, rng)

    items = []
    for amount, remark in zip(amounts, remarks):
        items.append({
            # ATM entries can land on any day, everything else Mon-Fri.
            "date": entry_date(rng, remark, every_day, working_days),
            "amount": amount,
            "remark": f"{remark} {ESTIMATED_TAG}",
            "source": "generated",
        })

    return items


# ----------------------- Known (uploaded) rows ---------------------------

def read_known_csv(uploaded_file) -> pd.DataFrame:
    """
    Read an uploaded CSV as text. Title lines above the column headings
    (e.g. a project header) are skipped: the heading row is the first row
    that has an 'amount' column or a debit/credit column. Comma, semicolon
    and tab separated files are all accepted.
    """
    data = uploaded_file.getvalue()
    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
    else:
        text = str(data)

    wanted = {"amount"} | set(KNOWN_DEBIT_COLUMNS) | set(KNOWN_CREDIT_COLUMNS)

    for delimiter in (",", ";", "\t"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        for index, row in enumerate(rows):
            header = [cell.strip() for cell in row]
            if not wanted & {cell.lower() for cell in header}:
                continue

            width = len(header)
            body = []
            for line in rows[index + 1:]:
                cells = [cell.strip() for cell in line]
                if not any(cells):
                    continue  # blank line
                cells = (cells + [""] * width)[:width]
                body.append(cells)
            return pd.DataFrame(body, columns=header, dtype=str)

    raise ValueError(
        "No column headings found. The CSV needs an 'amount' column, "
        "or 'Debits' and 'Credits' columns."
    )


def side_cents(value) -> int:
    """Read a debit or credit cell as positive cents; blank cells are 0."""
    if value is None or pd.isna(value):
        return 0
    text = str(value).strip()
    if text in ("", "-", "nan"):
        return 0
    return abs(to_cents(text))


def prepare_known_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """
    Validate an uploaded CSV and return it with tidy column names.

    Two layouts are accepted:
      * a signed 'amount' column (credits positive, debits negative), or
      * statement style 'Debits' / 'Credits' columns, which are turned
        into a signed 'amount' column. Footer rows (TOTAL, Total
        Transactions, Uncleared Amount ...) and rows with no debit or
        credit are left out.
    """
    frame = raw.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]

    if "amount" not in frame.columns:
        debit_column = next(
            (c for c in KNOWN_DEBIT_COLUMNS if c in frame.columns), None
        )
        credit_column = next(
            (c for c in KNOWN_CREDIT_COLUMNS if c in frame.columns), None
        )
        if debit_column is None and credit_column is None:
            raise ValueError(
                "The CSV must contain an 'amount' column, or 'Debits' "
                "and 'Credits' columns."
            )

        first_column = frame.columns[0]
        keep, amounts = [], []
        for position, (label, row) in enumerate(frame.iterrows(), start=1):
            first_cell = str(row[first_column]).strip().lower()
            if first_cell.startswith(NON_TRANSACTION_PREFIXES):
                continue  # footer / summary row

            try:
                debit = side_cents(row[debit_column]) if debit_column else 0
                credit = (
                    side_cents(row[credit_column]) if credit_column else 0
                )
            except ValueError:
                raise ValueError(
                    f"Row {position} has an invalid debit or credit."
                )

            if debit == 0 and credit == 0:
                continue  # nothing moved on this row

            net = credit - debit
            sign = "-" if net < 0 else ""
            keep.append(label)
            amounts.append(f"{sign}{Decimal(abs(net)) / Decimal(100):.2f}")

        frame = frame.loc[keep].reset_index(drop=True)
        frame["amount"] = amounts

        if frame.empty:
            raise ValueError("No transactions were found in the CSV.")

    for position, value in enumerate(frame["amount"], start=1):
        if pd.isna(value) or str(value).strip() == "":
            raise ValueError(f"Row {position} has no amount.")
        try:
            to_cents(value)
        except ValueError:
            raise ValueError(
                f"Row {position} has an invalid amount: {value}"
            )

    return frame


def known_net(frame: pd.DataFrame) -> int:
    return sum(to_cents(v) for v in frame["amount"])


def parse_date(text: str) -> date:
    """
    Parse a date. ISO dates (2026-02-10) are read year-month-day; all other
    formats (14-Feb-2026, 14/02/2026) are read day first.
    """
    text = text.strip()
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}", text):
        return pd.to_datetime(text).date()
    return pd.to_datetime(text, dayfirst=True).date()


def known_items_from_frame(frame: pd.DataFrame, default_date: date) -> list:
    """
    Turn uploaded rows into statement items. A date column is used when the
    CSV has one; otherwise rows are placed on `default_date`. A remarks
    column (remarks/details/description/narration) is used when present.
    """
    date_column = next(
        (c for c in KNOWN_DATE_COLUMNS if c in frame.columns), None
    )
    remark_column = next(
        (c for c in KNOWN_REMARK_COLUMNS if c in frame.columns), None
    )

    items = []
    for position, (_, row) in enumerate(frame.iterrows(), start=1):
        amount = to_cents(row["amount"])
        if amount == 0:
            continue  # a zero amount changes nothing

        row_date = default_date
        if date_column is not None and not pd.isna(row[date_column]) \
                and str(row[date_column]).strip() != "":
            try:
                row_date = parse_date(str(row[date_column]))
            except (ValueError, TypeError):
                raise ValueError(
                    f"Row {position} has an invalid date: "
                    f"{row[date_column]}"
                )

        remark = KNOWN_DEFAULT_REMARK
        if remark_column is not None and not pd.isna(row[remark_column]) \
                and str(row[remark_column]).strip() != "":
            remark = str(row[remark_column]).strip()

        items.append({
            "date": row_date,
            "amount": amount,
            "remark": remark,
            "source": "known",
        })

    return items


# ------------------------------ Salary -----------------------------------

def salary_dates(start_date: date, end_date: date) -> list:
    """
    The pay day of every month that falls inside the gap dates. Pay day is
    the 1st, or the Monday after it when the 1st is a Saturday or Sunday
    (deposits are not made on weekends).
    """
    dates = []
    year, month = start_date.year, start_date.month
    while date(year, month, 1) <= end_date:
        pay_day = date(year, month, 1)
        while is_weekend(pay_day):
            pay_day += timedelta(days=1)
        if start_date <= pay_day <= end_date:
            dates.append(pay_day)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return dates


def salary_items(amount_cents: int, start_date: date, end_date: date) -> list:
    """
    One salary credit of `amount_cents` on every pay day inside the gap
    dates (see salary_dates). Salary is money coming IN, so it is a credit.
    """
    if amount_cents <= 0:
        return []
    return [
        {
            "date": pay_day,
            "amount": amount_cents,
            "remark": SALARY_REMARK,
            "source": "salary",
        }
        for pay_day in salary_dates(start_date, end_date)
    ]


# ------------------------- Statement building ----------------------------

SOURCE_ORDER = {"known": 0, "salary": 1, "generated": 2}


def sort_items(items: list) -> list:
    """
    Sort by date; on the same date known rows come first, then salary,
    then estimates.
    """
    return sorted(
        items,
        key=lambda i: (i["date"], SOURCE_ORDER[i["source"]]),
    )


def lowest_balance(opening_cents: int, items: list) -> int:
    """The lowest running balance, counting the opening balance."""
    lowest = balance = opening_cents
    for item in items:
        balance += item["amount"]
        lowest = min(lowest, balance)
    return lowest


def finalize_items(
    opening_cents: int, fixed: list, generated: list, seed: int
) -> list:
    """
    Combine fixed entries (known and salary rows) and estimated entries in
    date order. The estimated amounts are re-ordered across their dates so
    the balance stays at or above zero whenever possible (like a real
    account). Totals and dates do not change, only which amount lands on
    which date. Fixed entries are never moved or changed, and a weekend
    date is only ever given an ATM entry.
    """
    rng = random.Random(seed + 1)
    items = [dict(i) for i in sort_items(fixed + generated)]

    pool = [(i["amount"], i["remark"]) for i in items
            if i["source"] == "generated"]
    weekend_dates_left = sum(
        1 for i in items
        if i["source"] == "generated" and is_weekend(i["date"])
    )

    balance = opening_cents
    for item in items:
        if item["source"] != "generated":
            balance += item["amount"]
            continue

        atm = {k for k, (_, r) in enumerate(pool) if allowed_on_weekend(r)}
        if is_weekend(item["date"]):
            # Only an ATM entry can go on a weekend.
            choices = sorted(atm)
            weekend_dates_left -= 1
        elif len(atm) > weekend_dates_left:
            choices = list(range(len(pool)))
        else:
            # Save the last ATM entries for the weekend dates still to come.
            choices = [k for k in range(len(pool)) if k not in atm]

        feasible = [k for k in choices if balance + pool[k][0] >= 0]
        if feasible:
            pick = rng.choice(feasible)
        else:
            # Nothing fits: take the largest credit (or the smallest debit).
            pick = max(choices, key=lambda k: pool[k][0])

        amount, remark = pool.pop(pick)
        item["amount"], item["remark"] = amount, remark
        balance += amount

    return items


def finalize_statement(
    opening_cents: int,
    fixed: list,
    generated: list,
    start_date: date,
    end_date: date,
    seed: int,
) -> list:
    """
    finalize_items, repeated when the balance would drop below zero.

    Fixed rows (known, salary) and the weekend rule limit where entries
    can go, so estimated debits can land before the money that covers
    them. When that happens the estimated entries get new random dates
    (same amounts and remarks, still following the weekend rule) and the
    statement is built again, keeping the attempt with the highest lowest
    balance. The same seed always gives the same result.
    """
    best = finalize_items(opening_cents, fixed, generated, seed)
    if not generated:
        return best

    closing_cents = opening_cents + sum(i["amount"] for i in best)
    # A negative opening or closing balance cannot be avoided.
    target = min(0, opening_cents, closing_cents)
    best_low = lowest_balance(opening_cents, best)
    every_day, working_days = gap_days(start_date, end_date)

    for attempt in range(1, DATE_RETRIES + 1):
        if best_low >= target:
            break
        rng = random.Random(seed * 1_000_003 + attempt)
        moved = [
            dict(item, date=entry_date(
                rng, item["remark"], every_day, working_days
            ))
            for item in generated
        ]
        candidate = finalize_items(opening_cents, fixed, moved, seed + attempt)
        candidate_low = lowest_balance(opening_cents, candidate)
        if candidate_low > best_low:
            best, best_low = candidate, candidate_low

    return best


def statement_frame(opening_cents: int, items: list) -> pd.DataFrame:
    """Build the statement table with a running balance."""
    rows = []
    balance = opening_cents

    for item in items:
        amount = item["amount"]
        balance += amount
        text_date = item["date"].strftime(DATE_FORMAT)
        rows.append({
            "Trans. Date": text_date,
            "Value. Date": text_date,
            "Debits": from_cents(-amount) if amount < 0 else "",
            "Credits": from_cents(amount) if amount > 0 else "",
            "Balance": from_cents(balance),
            "Remarks": item["remark"],
        })

    return pd.DataFrame(rows, columns=COLUMNS)


def build_totals(items: list, final_balance_cents: int) -> pd.DataFrame:
    """Build the TOTAL / Total Transactions / Uncleared Amount footer."""
    debits = [-i["amount"] for i in items if i["amount"] < 0]
    credits = [i["amount"] for i in items if i["amount"] > 0]

    blank = {column: "" for column in COLUMNS}

    total_row = dict(blank)
    total_row.update({
        "Trans. Date": "TOTAL",
        "Debits": from_cents(sum(debits)),
        "Credits": from_cents(sum(credits)),
        "Balance": from_cents(final_balance_cents),
    })

    count_row = dict(blank)
    count_row.update({
        "Trans. Date": "Total Transactions",
        "Debits": plural(len(debits), "Debit"),
        "Credits": plural(len(credits), "Credit"),
    })

    uncleared_row = dict(blank)
    uncleared_row.update({
        "Trans. Date": "Uncleared Amount",
        "Value. Date": "0",
    })

    return pd.DataFrame(
        [total_row, count_row, uncleared_row], columns=COLUMNS
    )


def verify_statement(
    opening_cents: int,
    closing_cents: int,
    missing_net: int,
    items: list,
    table: pd.DataFrame,
    debit_count: int,
    credit_count: int,
    start_date: date,
    end_date: date,
    salary_cents: int = 0,
) -> list:
    """Return a list of (passed, description) accuracy checks."""
    generated = [i for i in items if i["source"] == "generated"]
    salary = [i for i in items if i["source"] == "salary"]
    gen_debits = [-i["amount"] for i in generated if i["amount"] < 0]
    gen_credits = [i["amount"] for i in generated if i["amount"] > 0]

    checks = []

    # 1. Row-by-row running balance recomputed from the displayed table.
    balance = opening_cents
    running_ok = True
    for _, row in table.iterrows():
        if row["Debits"] != "":
            balance -= to_cents(row["Debits"])
        if row["Credits"] != "":
            balance += to_cents(row["Credits"])
        if to_cents(row["Balance"]) != balance:
            running_ok = False
            break
    checks.append((running_ok, "Every running balance in the table is correct"))

    # 2. Final balance equals the closing balance entered.
    final = to_cents(table["Balance"].iloc[-1]) if len(table) else opening_cents
    checks.append((
        final == closing_cents,
        f"Final balance ({from_cents(final)}) equals the closing balance "
        f"({from_cents(closing_cents)})",
    ))

    # 3. Estimated entries add up to the missing amount exactly.
    generated_net = sum(i["amount"] for i in generated)
    checks.append((
        generated_net == missing_net,
        f"Estimated entries net ({from_cents(generated_net)}) equals the "
        f"missing amount ({from_cents(missing_net)})",
    ))

    if missing_net != 0:
        # 4. Requested counts.
        checks.append((
            len(gen_debits) == debit_count
            and len(gen_credits) == credit_count,
            f"Estimated entries: {plural(len(gen_debits), 'debit')} and "
            f"{plural(len(gen_credits), 'credit')} as requested",
        ))

        # 5. Minimum sizes.
        checks.append((
            all(a >= MIN_DEBIT_CENTS for a in gen_debits)
            and all(a >= MIN_CREDIT_CENTS for a in gen_credits),
            f"Every estimated debit is at least "
            f"{from_cents(MIN_DEBIT_CENTS)} and every estimated credit is "
            f"at least {from_cents(MIN_CREDIT_CENTS)}",
        ))

        # 6. Estimated dates fall inside the gap.
        checks.append((
            all(start_date <= i["date"] <= end_date for i in generated),
            "Estimated entries are dated inside the gap dates",
        ))

    # Salary rows: one credit per month, on pay day, for the amount entered.
    if salary:
        checks.append((
            [i["date"] for i in salary] == salary_dates(start_date, end_date)
            and all(i["amount"] == salary_cents for i in salary)
            and all(i["remark"] == SALARY_REMARK for i in salary),
            f"Salary: {plural(len(salary), 'credit')} of "
            f"{from_cents(salary_cents)}, one each month on the 1st "
            f"(or the Monday after a weekend 1st)",
        ))

    # ATM withdrawals never have cents.
    atm_withdrawals = [
        -i["amount"] for i in generated
        if i["amount"] < 0 and allowed_on_weekend(i["remark"])
    ]
    if atm_withdrawals:
        checks.append((
            all(a % ATM_NOTE_CENTS == 0 for a in atm_withdrawals),
            f"Every ATM withdrawal is a whole amount with no cents "
            f"(a multiple of {from_cents(ATM_NOTE_CENTS)})",
        ))

    # Weekend rule (uploaded rows are left exactly as uploaded).
    if generated or salary:
        checks.append((
            all(
                allowed_on_weekend(i["remark"]) or not is_weekend(i["date"])
                for i in generated + salary
            ),
            "Only ATM entries fall on a Saturday or Sunday",
        ))

    # 7. Balance stays at or above zero (not checked with uploaded rows or
    #    a negative opening or closing balance). It holds whenever the
    #    estimated entries could be dated to fit around the pay days and
    #    the weekend rule.
    if (
        all(i["source"] != "known" for i in items)
        and opening_cents >= 0 and closing_cents >= 0
    ):
        lowest = lowest_balance(opening_cents, items)
        hint = "" if lowest >= 0 else " - try another random seed"
        checks.append((
            lowest >= 0,
            f"Balance never drops below zero "
            f"(lowest: {from_cents(lowest)}){hint}",
        ))

    # 8. No zero-amount rows.
    checks.append((
        all(i["amount"] != 0 for i in items),
        "No zero-amount rows",
    ))

    return checks


def show_table(frame: pd.DataFrame) -> None:
    """Display a table full width on both old and new Streamlit versions."""
    try:
        st.dataframe(frame, width="stretch")
    except TypeError:
        st.dataframe(frame, use_container_width=True)


# ------------------------------- App -------------------------------------

st.set_page_config(
    page_title="Balance Reconciliation",
    layout="wide"
)

st.markdown(
    f"**{PROJECT_HEADER}**  \n"
    f"Index Number: **{INDEX_NUMBER}**"
)

st.title("Balance Reconciliation Tool")

st.warning(
    "This tool calculates balance differences and creates estimated "
    "reconciliation entries. It cannot recover actual merchant names, "
    "dates, or transaction amounts from balances alone."
)

mode = st.selectbox(
    "Operation",
    [
        "Reconcile opening and closing balances",
        "Calculate closing balance",
        "Calculate opening balance",
    ]
)

st.caption(
    "Amounts use positive numbers for credits/deposits and "
    "negative numbers for debits/withdrawals."
)

uploaded_file = st.file_uploader(
    "Optional known-transactions CSV",
    type=["csv"],
    help=(
        "Needs an 'amount' column, or 'Debits' and 'Credits' columns "
        "(a downloaded statement_entries.csv works as it is). "
        "Optional columns: 'date' (any common "
        "format, e.g. 2026-02-14 or 14-Feb-2026) and 'remarks', 'details', "
        "'description' or 'narration'. Rows without a date are placed on "
        "the gap start date."
    ),
)

known_frame = pd.DataFrame()
known_net_cents = 0

if uploaded_file is not None:
    try:
        known_frame = prepare_known_frame(read_known_csv(uploaded_file))
        known_net_cents = known_net(known_frame)

        st.success(
            f"Known transaction net: "
            f"{money(known_net_cents)}"
        )
        show_table(known_frame)

    except Exception as exc:
        st.error(f"Could not read the CSV: {exc}")
        st.stop()


if mode == "Calculate closing balance":
    opening_text = st.text_input("Opening balance", "1000.00")

    if st.button("Calculate closing balance"):
        try:
            opening_cents = to_cents(opening_text)
            closing_cents = opening_cents + known_net_cents

            st.metric(
                "Calculated closing balance",
                f"{money(closing_cents)}"
            )
        except ValueError as exc:
            st.error(str(exc))


elif mode == "Calculate opening balance":
    closing_text = st.text_input("Closing balance", "1000.00")

    if st.button("Calculate opening balance"):
        try:
            closing_cents = to_cents(closing_text)
            opening_cents = closing_cents - known_net_cents

            st.metric(
                "Calculated opening balance",
                f"{money(opening_cents)}"
            )
        except ValueError as exc:
            st.error(str(exc))


else:
    left, right = st.columns(2)

    with left:
        opening_text = st.text_input(
            "Opening balance",
            "1000.00"
        )
        start_date = st.date_input(
            "Gap start date",
            value=date.today()
        )

    with right:
        closing_text = st.text_input(
            "Closing balance",
            "1250.00"
        )
        end_date = st.date_input(
            "Gap end date",
            value=date.today()
        )

    debit_col, credit_col = st.columns(2)

    with debit_col:
        debit_count = st.number_input(
            "Number of debits",
            min_value=0,
            max_value=100,
            value=6,
            step=1,
        )

    with credit_col:
        credit_count = st.number_input(
            "Number of credits",
            min_value=0,
            max_value=100,
            value=2,
            step=1,
        )

    salary_text = st.text_input(
        "Monthly salary (optional)",
        "0.00",
        help=(
            "Added as a credit on the 1st of every month between the gap "
            "dates (the following Monday when the 1st is on a weekend), "
            f"with the remark '{SALARY_REMARK}'. Salary credits are "
            "extra to the number of credits above. Leave at 0 for no salary."
        ),
    )

    seed = st.number_input(
        "Random seed",
        min_value=0,
        value=1,
        step=1,
        help="Use the same seed to reproduce the same result."
    )

    if st.button("Reconcile balances", type="primary"):
        try:
            opening_cents = to_cents(opening_text)
            closing_cents = to_cents(closing_text)

            if end_date < start_date:
                raise ValueError("End date cannot be before start date.")

            salary_cents = to_cents(salary_text) if salary_text.strip() else 0
            if salary_cents < 0:
                raise ValueError("Monthly salary cannot be negative.")

            salary_entries = salary_items(salary_cents, start_date, end_date)
            salary_total = sum(i["amount"] for i in salary_entries)

            expected_change = closing_cents - opening_cents
            missing_net = expected_change - known_net_cents - salary_total

            summary = st.columns(4 if salary_entries else 3)

            summary[0].metric(
                "Total balance change",
                f"{money(expected_change)}"
            )
            summary[1].metric(
                "Known transaction net",
                f"{money(known_net_cents)}"
            )
            if salary_entries:
                summary[2].metric(
                    f"Salary ({plural(len(salary_entries), 'credit')})",
                    f"{money(salary_total)}"
                )
            summary[-1].metric(
                "Missing net amount",
                f"{money(missing_net)}"
            )

            if salary_cents > 0 and not salary_entries:
                st.info(
                    "No pay day (the 1st of a month, or the Monday after "
                    "a weekend 1st) falls between the gap dates, so no "
                    "salary entry was added."
                )

            known_items = known_items_from_frame(known_frame, start_date) \
                if len(known_frame) else []

            generated_items = generate_items(
                net_cents=missing_net,
                debit_count=int(debit_count),
                credit_count=int(credit_count),
                start_date=start_date,
                end_date=end_date,
                seed=int(seed),
            )

            items = finalize_statement(
                opening_cents, known_items + salary_entries,
                generated_items, start_date, end_date, int(seed),
            )

            if not items:
                st.success(
                    "The opening and closing balances already match. "
                    "There is nothing to generate."
                )
            else:
                if missing_net == 0:
                    st.success(
                        "The known transactions and salary entries already "
                        "reconcile the balances, so no estimated entries "
                        "were added."
                        if salary_entries else
                        "The known transactions already reconcile the "
                        "balances, so no estimated entries were added."
                    )

                table = statement_frame(opening_cents, items)
                final_balance = opening_cents + sum(
                    i["amount"] for i in items
                )
                with_totals = pd.concat(
                    [table, build_totals(items, final_balance)],
                    ignore_index=True,
                )

                st.markdown(
                    f"**{PROJECT_HEADER}** | Index Number: {INDEX_NUMBER}"
                )
                st.subheader("Statement entries")
                show_table(with_totals)

                st.subheader("Accuracy checks")
                checks = verify_statement(
                    opening_cents=opening_cents,
                    closing_cents=closing_cents,
                    missing_net=missing_net,
                    items=items,
                    table=table,
                    debit_count=int(debit_count),
                    credit_count=int(credit_count),
                    start_date=start_date,
                    end_date=end_date,
                    salary_cents=salary_cents,
                )
                for passed, description in checks:
                    if passed:
                        st.success(description)
                    else:
                        st.error(f"FAILED: {description}")

                generated_only = [
                    i for i in items if i["source"] == "generated"
                ]
                big_debits = sum(
                    1 for i in generated_only
                    if -i["amount"] > BIG_THRESHOLD_CENTS
                )
                big_credits = sum(
                    1 for i in generated_only
                    if i["amount"] > BIG_THRESHOLD_CENTS
                )
                if generated_only:
                    st.caption(
                        f"Estimated debits above "
                        f"{from_cents(BIG_THRESHOLD_CENTS)}: {big_debits}. "
                        f"Estimated credits above "
                        f"{from_cents(BIG_THRESHOLD_CENTS)}: {big_credits}."
                    )

                st.download_button(
                    "Download statement entries",
                    data=(
                        f"{PROJECT_HEADER}\n"
                        f"Index Number: {INDEX_NUMBER}\n\n"
                        + with_totals.to_csv(index=False)
                    ),
                    file_name="statement_entries.csv",
                    mime="text/csv",
                )

        except Exception as exc:
            st.error(str(exc))
