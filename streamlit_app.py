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

ESTIMATED_TAG = "(ESTIMATED / NOT BANK-SOURCED)"
KNOWN_DEFAULT_REMARK = "KNOWN TRANSACTION (UPLOADED)"

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

    number_of_days = (end_date - start_date).days
    items = []
    for amount in amounts:
        pool = DEBIT_REMARKS if amount < 0 else CREDIT_REMARKS
        items.append({
            "date": start_date + timedelta(
                days=rng.randint(0, number_of_days)
            ),
            "amount": amount,
            "remark": f"{rng.choice(pool)} {ESTIMATED_TAG}",
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


# ------------------------- Statement building ----------------------------

def sort_items(items: list) -> list:
    """Sort by date; on the same date known rows come before estimates."""
    return sorted(
        items,
        key=lambda i: (i["date"], 0 if i["source"] == "known" else 1),
    )


def finalize_items(
    opening_cents: int, known: list, generated: list, seed: int
) -> list:
    """
    Combine known and estimated entries in date order. The estimated
    amounts are re-ordered across their dates so the balance stays at or
    above zero whenever possible (like a real account). Totals and dates
    do not change, only which amount lands on which date.
    """
    rng = random.Random(seed + 1)
    items = [dict(i) for i in sort_items(known + generated)]

    pool = [(i["amount"], i["remark"]) for i in items
            if i["source"] == "generated"]

    balance = opening_cents
    for item in items:
        if item["source"] == "known":
            balance += item["amount"]
            continue

        feasible = [k for k, (a, _) in enumerate(pool) if balance + a >= 0]
        if feasible:
            pick = rng.choice(feasible)
        else:
            # Nothing fits: take the largest credit (or the smallest debit).
            pick = max(range(len(pool)), key=lambda k: pool[k][0])

        amount, remark = pool.pop(pick)
        item["amount"], item["remark"] = amount, remark
        balance += amount

    return items


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
) -> list:
    """Return a list of (passed, description) accuracy checks."""
    generated = [i for i in items if i["source"] == "generated"]
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

    # 7. Balance stays at or above zero (only checked when it is possible
    #    to guarantee: no uploaded rows, non-negative opening and closing).
    if (
        all(i["source"] == "generated" for i in items)
        and opening_cents >= 0 and closing_cents >= 0
    ):
        lowest = opening_cents
        balance = opening_cents
        for i in items:
            balance += i["amount"]
            lowest = min(lowest, balance)
        checks.append((
            lowest >= 0,
            f"Balance never drops below zero (lowest: {from_cents(lowest)})",
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

            expected_change = closing_cents - opening_cents
            missing_net = expected_change - known_net_cents

            summary_1, summary_2, summary_3 = st.columns(3)

            summary_1.metric(
                "Total balance change",
                f"{money(expected_change)}"
            )
            summary_2.metric(
                "Known transaction net",
                f"{money(known_net_cents)}"
            )
            summary_3.metric(
                "Missing net amount",
                f"{money(missing_net)}"
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

            items = finalize_items(
                opening_cents, known_items, generated_items, int(seed)
            )

            if not items:
                st.success(
                    "The opening and closing balances already match. "
                    "There is nothing to generate."
                )
            else:
                if missing_net == 0:
                    st.success(
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
