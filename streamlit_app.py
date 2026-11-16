from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import date, timedelta
import csv
import html
import io
import math
import random
import re

import pandas as pd
import streamlit as st

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas as pdfcanvas
    PDF_AVAILABLE = True
except ImportError:
    # Without the reportlab package the app still works, minus the PDF.
    PDF_AVAILABLE = False


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

# Account holder shown on the statement.
CUSTOMER_NAME = "SABAR GARBA NYAMKENE"

# Printed on every page of the PDF so it cannot be taken for a bank record.
PDF_SUBTITLE = "Simulated statement entries - sample data, not a bank document"
PDF_WATERMARK = "SAMPLE - NOT A BANK DOCUMENT"
PDF_FOOTER = "SCHOOL PROJECT  |  SAMPLE DATA  |  NOT A BANK DOCUMENT"
PDF_NOTE = (
    "Rows marked (ESTIMATED / NOT BANK-SOURCED) were created by this tool "
    "to reconcile the opening and closing balances. This document is a "
    "school project sample. It is not issued by any bank and is not a "
    "record of real transactions."
)

COLUMNS = [
    "Trans. Date", "Value. Date", "Debits",
    "Credits", "Balance", "Remarks"
]

# The monthly card charge is always exactly this amount (500 = 5.00).
CARD_FEE_REMARK = "CHARGES WS 10 OCTOBER 2025 VISA MONTHLY FEES 19/10/2025"
CARD_FEE_CENTS = 500

DEBIT_REMARKS = [
    f"CASH WITHDRAWAL {CUSTOMER_NAME}",
    "CASH WITHDRAWAL FROM OUR ATM GTB000000000770 KASOA 00228KASOA KASOA",
    CARD_FEE_REMARK,
    "POS TRANSACTION FEE ZOOM.US 888-769-9686 0000144 2026-10-20 CA 206.00 USD",
    "CASH WITHDRAWAL FROM OUR ATM GTB000000000901 KASOA 00228KASOA KASOA",
]

# Debit remarks whose amount is free (everything but the card charge).
OTHER_DEBIT_REMARKS = [r for r in DEBIT_REMARKS if r != CARD_FEE_REMARK]

CREDIT_REMARKS = [
    f"CASH DEPOSITE {CUSTOMER_NAME}",     # GTBank spells it this way
    f"TRANSFER IN {CUSTOMER_NAME}",
]

# Weekend rule: on Saturdays and Sundays only ATM transactions are possible.
# A remark is an ATM transaction when it contains the word "ATM", so any
# remark added above with "ATM" in it may also fall on a weekend.

# An ATM never pays out cents: every ATM withdrawal is a whole number such
# as 20.00, 48.00 or 51.00, never 20.83. The value is the step in cents
# (100 = whole numbers; 1000 would allow only multiples of 10.00).
ATM_NOTE_CENTS = 100

ESTIMATED_TAG = ""
KNOWN_DEFAULT_REMARK = "KNOWN TRANSACTION (UPLOADED)"
SALARY_REMARK = "SALARY"
SALARY_FIRST_DAY = 25       # salary is paid from the 25th to the month end
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

    other_remarks = [
        r for r in OTHER_DEBIT_REMARKS if not allowed_on_weekend(r)
    ]
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


def generate_amounts(
    net_cents: int, debit_count: int, credit_count: int, rng: random.Random
) -> list:
    """
    Exactly `debit_count` debit amounts (negative cents) and `credit_count`
    credit amounts (positive cents), in random order, that add up to
    net_cents. Raises ValueError when that is not possible.
    """
    if net_cents == 0:
        if debit_count + credit_count == 0:
            return []
        raise ValueError("Entries that add up to zero cannot be generated.")

    magnitude = abs(net_cents)

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
    return amounts


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
    negative), remark and source. Monthly card charges are always exactly
    CARD_FEE_CENTS and ATM withdrawals never have cents.
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

    rng = random.Random(seed)

    # Each debit has the same chance as before of being a monthly card
    # charge. Those are always exactly CARD_FEE_CENTS, so they are set
    # aside first and the other entries make up the rest of the amount.
    # If the rest cannot be generated, fewer card charges are used.
    fee_count = sum(
        1 for _ in range(debit_count)
        if rng.choice(DEBIT_REMARKS) == CARD_FEE_REMARK
    )
    while True:
        try:
            amounts = generate_amounts(
                net_cents + fee_count * CARD_FEE_CENTS,
                debit_count - fee_count,
                credit_count,
                rng,
            )
            break
        except ValueError:
            if fee_count == 0:
                raise
            fee_count -= 1

    remarks = [
        rng.choice(OTHER_DEBIT_REMARKS if amount < 0 else CREDIT_REMARKS)
        for amount in amounts
    ]
    amounts, remarks = round_atm_withdrawals(amounts, remarks, rng)

    amounts += [-CARD_FEE_CENTS] * fee_count
    remarks += [CARD_FEE_REMARK] * fee_count

    items = []
    for amount, remark in zip(amounts, remarks):
        items.append({
            # ATM entries can land on any day, everything else Mon-Fri.
            "date": entry_date(rng, remark, every_day, working_days),
            "amount": amount,
            "remark": f"{remark} {ESTIMATED_TAG}".strip(),
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
    frame.columns = [str(c).strip().lower()