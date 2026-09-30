"""What a print costs, and what to charge. In lei.

The operator, 2026-09-05: "if I make something that takes 30 hours to print and
500 grams of PETG that cost me 70 lei per kg, did I actually have a thing where
I know how much to charge per print?"

He did not, and the reason is the first test here. `calculate_job_economics`
existed and was correct arithmetic, but `cli_dispatch` built its `PrintJob`
without ever passing `estimated_time_seconds`, so every job in the product
was costed at the dataclass default of 1800 seconds. Measured on his job:

    quoted   $32.03   (costed as a 30-minute print)
    truth    $67.26   (costed as a 30-hour print)

He would have believed that price carried 65% margin. On the real cost it
carries 27%. Nothing failed, nothing warned, and the number looked reasonable
-- which is what makes it worth a test rather than a fix.
"""
from __future__ import annotations

import pytest

from aethelark3d import costing
from aethelark3d.costing import Rates, quote

#: The operator's job.
GRAMS, HOURS = 500.0, 30.0
SECONDS = int(HOURS * 3600)

#: His two real figures.
HIS = Rates(electricity_lei_per_kwh=2.0, filament_lei_per_kg=70.0,
            provided={"electricity_lei_per_kwh", "filament_lei_per_kg"})


# --------------------------------------------------------- the original bug

def test_a_thirty_hour_print_does_not_cost_what_a_thirty_minute_one_costs():
    long_run = quote(GRAMS, SECONDS, HIS)
    short_run = quote(GRAMS, 1800, HIS)
    assert long_run["cost"] > short_run["cost"] * 1.4, (
        "an hour of printing added almost nothing to the cost, which is the "
        "shape of the bug: the duration was never reaching the arithmetic")


def test_the_dispatch_command_passes_the_real_print_time():
    """The defect was in the caller, not the calculator."""
    import inspect

    from aethelark3d import cli

    src = inspect.getsource(cli.cli_dispatch)
    assert "estimated_time_seconds" in src, (
        "cli_dispatch builds its PrintJob without a print time, so every job "
        "is costed as the 1800-second default no matter how long it runs")


def test_time_only_costs_scale_with_time():
    one = quote(GRAMS, 3600, HIS)
    ten = quote(GRAMS, 36000, HIS)

    def part(result, what):
        return next(r["amount"] for r in result["breakdown"] if r["what"] == what)

    assert part(ten, "electricity") == pytest.approx(part(one, "electricity") * 10, rel=0.02)
    assert part(ten, "machine wear") == pytest.approx(part(one, "machine wear") * 10, rel=0.02)
    assert part(ten, "filament") == pytest.approx(part(one, "filament"), rel=0.001), (
        "filament is bought by weight; running longer does not consume more of it")


# ------------------------------------------------------------- the arithmetic

def test_material_is_weight_times_the_price_of_that_material():
    result = quote(1000.0, SECONDS, HIS, material="PETG")
    filament = next(r for r in result["breakdown"] if r["what"] == "filament")
    assert filament["amount"] == pytest.approx(70.0), (
        "one kilo at 70 lei/kg is 70 lei")


def test_a_material_with_its_own_price_beats_the_general_one():
    rates = Rates(filament_lei_per_kg=70.0, filament_prices={"PLA": 55.0})
    assert rates.price_per_kg("PLA") == 55.0
    assert rates.price_per_kg("pla") == 55.0, "matching must not be case-sensitive"
    assert rates.price_per_kg("ABS") == 70.0, "an unpriced material falls back"


def test_electricity_is_watts_times_hours_times_the_rate():
    rates = Rates(printer_watts=1000.0, electricity_lei_per_kwh=2.0)
    result = quote(1.0, 3600, rates)          # one kW for one hour = 1 kWh
    power = next(r for r in result["breakdown"] if r["what"] == "electricity")
    assert power["amount"] == pytest.approx(2.0)


def test_the_margin_is_taken_on_the_selling_price_not_added_to_cost():
    """65% margin means 65% OF THE PRICE, which is what a shop means.

    Adding 65% to cost would give a 39% margin. On this job that is the
    difference between 216 lei and 125 lei.
    """
    rates = Rates(margin_percent=65.0)
    result = quote(GRAMS, SECONDS, rates)
    cost, price = result["cost"], result["price"]
    assert (price - cost) / price == pytest.approx(0.65, abs=0.005)


def test_a_failure_rate_prices_attempts_not_deliveries():
    """Ten percent of runs failing costs 1/0.9 of the direct cost, not 1.1x.

    You buy 1/(1-f) attempts per delivered part. The difference compounds
    exactly where it hurts: long, expensive prints.
    """
    perfect = quote(GRAMS, SECONDS, Rates(failure_rate=0.0))
    lossy = quote(GRAMS, SECONDS, Rates(failure_rate=0.10))
    assert lossy["cost"] == pytest.approx(perfect["cost"] / 0.9, rel=0.01)


def test_a_hopeless_failure_rate_does_not_produce_an_infinite_price():
    result = quote(GRAMS, SECONDS, Rates(failure_rate=1.0))
    assert result["ok"] is True
    assert result["price"] > 0


def test_everything_is_lei():
    result = quote(GRAMS, SECONDS, HIS)
    assert result["currency"] == "lei"
    assert not [k for k in result if "usd" in k.lower()], (
        "a dollar figure survived into a quote denominated in lei")


# --------------------------------------------------- refusing to invent one

@pytest.mark.parametrize("grams, seconds", [(None, SECONDS), (GRAMS, None),
                                            (0, SECONDS), (GRAMS, 0),
                                            (None, None)])
def test_a_missing_number_is_said_not_guessed(grams, seconds):
    result = quote(grams, seconds, HIS)
    assert result["ok"] is False
    assert "price" not in result, (
        "a price was produced from a number nobody has; this is the defect "
        "that made the original quote wrong")


def test_the_refusal_says_what_to_do_about_it():
    """Read by Gemini 2.5 Flash mid-sentence: it needs the next step."""
    result = quote(None, None, HIS)
    guidance = result["guidance"].lower()
    assert "3mf" in guidance and "grams" in guidance
    assert "exception" not in guidance and "none" not in guidance.split()


# ----------------------------------------------------- assumed versus known

def test_a_quote_says_which_of_its_numbers_were_assumed():
    result = quote(GRAMS, SECONDS, HIS)
    assumed = result["assumed"]
    assert "electricity" not in assumed, "he gave the electricity rate himself"
    assert "filament price" not in assumed, "he gave the filament price himself"
    assert "printer wear" in assumed, (
        "the printer's price is a guess about his wallet and must say so")


def test_nothing_is_assumed_once_everything_is_configured():
    everything = Rates(provided=set(vars(Rates())) - {"provided"})
    assert quote(GRAMS, SECONDS, everything)["assumed"] == []


def test_the_spoken_line_names_the_price_the_floor_and_the_guesses():
    said = costing.say(quote(GRAMS, SECONDS, HIS))
    assert "lei" in said
    assert "assumed" in said.lower(), (
        "an assumed figure repeated to a customer as fact is how someone "
        "loses money; the sentence has to carry the caveat")


def test_the_spoken_line_for_a_refusal_is_the_reason():
    said = costing.say(quote(None, None, HIS))
    assert "lei" not in said.lower() or "no " in said.lower()


# ------------------------------------------------------------ configuration

def test_rates_remember_which_figures_a_person_actually_set():
    rates = Rates.from_config({"electricity_lei_per_kwh": 2.0})
    assert rates.electricity_lei_per_kwh == 2.0
    assert "electricity_lei_per_kwh" in rates.provided
    assert "printer_price_lei" not in rates.provided


def test_a_junk_config_value_does_not_become_a_price():
    rates = Rates.from_config({"electricity_lei_per_kwh": "free"})
    assert rates.electricity_lei_per_kwh == costing.DEFAULT_ELECTRICITY_LEI_PER_KWH
    assert "electricity_lei_per_kwh" not in rates.provided, (
        "an unreadable value was recorded as the user's own figure")


def test_an_empty_config_still_quotes():
    result = quote(GRAMS, SECONDS, Rates.from_config(None))
    assert result["ok"] is True and result["price"] > 0


def test_another_currency_needs_the_users_own_figures_first():
    """The defaults are Romanian prices in lei. Quoting in EUR on top of them
    would be wrong by an exchange rate, and a mix of the two is in no currency
    at all -- so the user's own money figures come first."""
    rates = costing.Rates.from_config({"currency": "EUR", "filament_lei_per_kg": 25})
    result = costing.quote(grams=100, seconds=3600, rates=rates)
    assert result["ok"] is False
    assert result["currency"] == "EUR"
    assert "the electricity price" in result["message"]
    assert "the filament price" not in result["message"]      # given


def test_with_their_figures_it_prices_in_their_currency():
    rates = costing.Rates.from_config({
        "currency": "EUR", "filament_lei_per_kg": 25, "electricity_lei_per_kwh": 0.3,
        "printer_price_lei": 400, "labour_lei_per_hour": 20})
    result = costing.quote(grams=100, seconds=3600, rates=rates)
    assert result["ok"] is True and result["currency"] == "EUR"
    assert "EUR" in costing.say(result) and "lei" not in costing.say(result)
