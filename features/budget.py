import re, json, calendar as _calendar
from config import now_jkt, PAYROLL_DAY, FIXED_EXPENSES, VARIABLE_BUDGETS
from ai.groq_client import groq_complete
from tracer import trace


# ================================================================
# AI BUDGET INPUT PARSER
# ================================================================
@trace
def _parse_budget_input(user_input: str) -> dict | None:
    now   = now_jkt()
    today = now.day
    month = now.strftime("%B")
    year  = now.year
    fixed_names    = ", ".join(e["name"] for e in FIXED_EXPENSES)
    variable_names = ", ".join(v["name"] for v in VARIABLE_BUDGETS)

    prompt = f"""You are a budget parser for a personal finance chatbot. Today is the {today}th of {month} {year}.

The user has these fixed monthly expenses: {fixed_names}
The user has these variable monthly budgets: {variable_names}

Extract:
1. "remaining_money": total money right now (integer IDR)
2. "paid_fixed": list of fixed expense names already paid this month
3. "spent_variable": dict of variable budget name → amount spent
4. "pending_conditional": list of conditional expense names still expected this month

Reply ONLY with valid JSON, no markdown:
{{"remaining_money": <int or null>, "paid_fixed": [<names>], "spent_variable": {{"<name>": <amount>}}, "pending_conditional": [<names>]}}

User message: {user_input}"""

    try:
        raw = groq_complete(
            system_prompt="You are a budget parser. Reply with valid JSON only.",
            user_prompt=prompt,
            max_tokens=300,
            temperature=0.0,
        )
        raw = re.sub(r"```json|```", "", raw).strip()
        return json.loads(raw)
    except Exception as e:
        print(f"[Budget parse error] {e}")
        return None

# ================================================================
# INTERACTIVE PROMPT (when no numbers given)
# ================================================================
@trace
def _budget_interactive_prompt() -> str:
    now       = now_jkt()
    today     = now.day
    days_left = (PAYROLL_DAY - today) if today < PAYROLL_DAY else (31 - today + PAYROLL_DAY)
    lines = [
        f"💰 *Budget Calculator* — {days_left} days until payday (25th)\n",
        "Please tell me:",
        "1️⃣ How much money do you have right now?",
        "2️⃣ Which fixed expenses have you already paid?",
        f"   Options: {', '.join(e['name'] for e in FIXED_EXPENSES)}",
        "3️⃣ How much have you spent from variable budgets?",
        f"   Options: {', '.join(v['name'] for v in VARIABLE_BUDGETS)}",
        "4️⃣ Any conditional expenses still pending? (e.g. Internet)",
        "",
        "💡 *Example:*",
        "\"I have 2.500.000. Already paid: Rent, Zakat, House Maintenance.",
        "Spent: Ticket 300k, Fuel 35k, Laundry 35k. Internet still pending.\"",
    ]
    return "\n".join(lines)

# ================================================================
# MAIN CALCULATOR
# ================================================================
@trace
def calculate_budget(user_input: str) -> str:
    now   = now_jkt()
    today = now.day

    if today <= PAYROLL_DAY:
        days_left = PAYROLL_DAY - today
    else:
        days_in_month = _calendar.monthrange(now.year, now.month)[1]
        days_left = (days_in_month - today) + PAYROLL_DAY

    bare_triggers = {"budget", "hitung budget", "kalkulasi budget", "budget calculator",
                     "budget harian", "sisa budget", "budget check"}
    if user_input.strip().lower() in bare_triggers:
        return _budget_interactive_prompt()

    parsed = _parse_budget_input(user_input)
    if not parsed or parsed.get("remaining_money") is None:
        return _budget_interactive_prompt()

    remaining      = parsed.get("remaining_money", 0)
    paid_fixed     = [n.lower() for n in (parsed.get("paid_fixed") or [])]
    spent_variable = {k.lower(): v for k, v in (parsed.get("spent_variable") or {}).items()}
    pending_cond   = [n.lower() for n in (parsed.get("pending_conditional") or [])]

    still_owed = [
        exp for exp in FIXED_EXPENSES
        if not any(exp["name"].lower() in p or p in exp["name"].lower() for p in paid_fixed)
    ]

    remaining_var = []
    for var in VARIABLE_BUDGETS:
        name_lower = var["name"].lower()
        spent = 0
        for k, v in spent_variable.items():
            if name_lower in k or k in name_lower:
                spent = v
                break
        leftover = var["budget"] - spent
        if leftover > 0:
            remaining_var.append({"name": var["name"], "remaining": leftover, "spent": spent})

    pending_amounts = [
        exp for exp in FIXED_EXPENSES
        if any(exp["name"].lower() in p or p in exp["name"].lower() for p in pending_cond)
        and not any(e["name"].lower() == exp["name"].lower() for e in still_owed)
    ]

    total_still_owed    = sum(e["amount"] for e in still_owed) + sum(e["amount"] for e in pending_amounts)
    total_var_remaining = sum(v["remaining"] for v in remaining_var)
    total_deductions    = total_still_owed + total_var_remaining
    free_money          = remaining - total_deductions
    daily_budget        = free_money / days_left if days_left > 0 else free_money

    def fmt(n):
        return f"Rp {int(n):,}".replace(",", ".")

    lines = [f"💰 *Budget Breakdown* — {days_left} days to payday (25th)\n"]
    lines.append(f"💵 Current money: *{fmt(remaining)}*\n")

    if still_owed or pending_amounts:
        lines.append("📋 *Fixed expenses still to pay:*")
        for e in still_owed:
            lines.append(f"  • {e['name']}: {fmt(e['amount'])}")
        for e in pending_amounts:
            lines.append(f"  • {e['name']} (pending): {fmt(e['amount'])}")
        lines.append(f"  ➤ Total: {fmt(total_still_owed)}\n")

    if remaining_var:
        lines.append("🗂️ *Remaining variable budgets:*")
        for v in remaining_var:
            lines.append(f"  • {v['name']}: {fmt(v['remaining'])} (spent {fmt(v['spent'])})")
        lines.append(f"  ➤ Total: {fmt(total_var_remaining)}\n")

    lines.append("📊 *Summary:*")
    lines.append(f"  Money in hand:      {fmt(remaining)}")
    lines.append(f"  Total deductions:   -{fmt(total_deductions)}")
    lines.append(f"  Free money left:    {fmt(free_money)}")
    lines.append(f"  Days until payday:  {days_left} days\n")

    if daily_budget < 0:
        lines.append(f"⚠️ *You're short by {fmt(abs(free_money))}!*")
        lines.append("Consider reducing variable spending.")
    else:
        lines.append(f"✅ *Daily budget: {fmt(daily_budget)}/day*")
        if daily_budget < 50_000:
            lines.append("⚠️ Tight! Keep non-essentials minimal.")
        elif daily_budget < 100_000:
            lines.append("🟡 Manageable. Watch your spending.")
        else:
            lines.append("🟢 You're in a comfortable position!")

    return "\n".join(lines)
