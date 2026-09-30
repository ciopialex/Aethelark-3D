"""What a print costs, and what to charge for it. In lei.

Asked by the operator on 2026-09-05: "if I make something that takes 30 hours
to print and 500 grams of PETG that cost me 70 lei per kg, did I actually have
a thing where I know how much to charge per print?"

He did not. `FleetDispatcher.calculate_job_economics` existed, but the command
that called it never passed the print time, so every job was costed as if it
took the `PrintJob` default of 1800 seconds. On his 30-hour job that quoted
$32.03 against a true cost of $23.54 -- a price he would have believed carried
65% margin, actually carrying 27%. It was also denominated in dollars at
someone else's filament price, and no tool exposed it to the eagle.

The arithmetic, from first principles. Every term is a real outflow:

    material     = kg x price per kg
    electricity  = hours x kW x price per kWh
    machine wear = hours x (what the printer cost / hours it will last)
    labour       = minutes of human handling x rate

Then the one everybody forgets. If a fraction `f` of prints fail and have to be
run again, the expected cost of one DELIVERED part is not the cost of one
attempt -- it is

    direct / (1 - f)

because on average you buy 1/(1-f) attempts per delivered part. At a 30-hour
job that is not a rounding error, and leaving it out is what turns a quote into
a loss.

Price is then cost / (1 - margin), which is margin ON THE SELLING PRICE -- the
sense a shop means it in. Adding 65% to cost would give 38% margin, and the
difference is the whole business.

**Nothing here invents a number and hides it.** Every figure carries whether it
came from the operator or from a default, so a quote can say which parts of
itself are measured and which are assumed. `docs/VISION.md`: no card states a
number nobody measured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

#: The operator's own figures, given 2026-09-05. These are the only two that
#: are not guesses, and the quote says so.
DEFAULT_ELECTRICITY_LEI_PER_KWH = 2.0
DEFAULT_FILAMENT_LEI_PER_KG = 70.0

#: Assumptions. Serviceable, plainly wrong for somebody else, and each one
#: replaceable with a single command. They are labelled `assumed` in the
#: output rather than presented as fact.
DEFAULT_PRINTER_WATTS = 120.0
DEFAULT_PRINTER_PRICE_LEI = 2000.0
DEFAULT_PRINTER_LIFETIME_HOURS = 4000.0
DEFAULT_FAILURE_RATE = 0.08
DEFAULT_LABOUR_MINUTES = 15.0
DEFAULT_LABOUR_LEI_PER_HOUR = 50.0
DEFAULT_MARGIN_PERCENT = 65.0


@dataclass
class Rates:
    """What things cost this operator.

    Every figure is in `currency`. The defaults are Romanian prices in lei, which
    is why a quote in any other currency needs the user's own figures first --
    see `quote`.
    """

    currency: str = "lei"

    electricity_lei_per_kwh: float = DEFAULT_ELECTRICITY_LEI_PER_KWH
    filament_lei_per_kg: float = DEFAULT_FILAMENT_LEI_PER_KG
    #: Per-material overrides, e.g. {"PETG": 70, "PLA": 55}. A material with no
    #: entry falls back to `filament_lei_per_kg`.
    filament_prices: Dict[str, float] = field(default_factory=dict)
    printer_watts: float = DEFAULT_PRINTER_WATTS
    printer_price_lei: float = DEFAULT_PRINTER_PRICE_LEI
    printer_lifetime_hours: float = DEFAULT_PRINTER_LIFETIME_HOURS
    failure_rate: float = DEFAULT_FAILURE_RATE
    labour_minutes: float = DEFAULT_LABOUR_MINUTES
    labour_lei_per_hour: float = DEFAULT_LABOUR_LEI_PER_HOUR
    margin_percent: float = DEFAULT_MARGIN_PERCENT

    #: Which fields the operator set himself. Everything else is a default and
    #: is reported as assumed.
    provided: set = field(default_factory=set)

    @classmethod
    def from_config(cls, stored: Optional[Dict[str, Any]]) -> "Rates":
        """Rates as configured, remembering which ones were actually set."""
        stored = stored if isinstance(stored, dict) else {}
        rates = cls()
        for name in vars(rates):
            if name == "provided" or name not in stored:
                continue
            value = stored[name]
            if name == "currency":
                if str(value).strip():
                    rates.currency = str(value).strip()
                continue
            if name == "filament_prices":
                if isinstance(value, dict):
                    rates.filament_prices = {str(k).upper(): float(v)
                                             for k, v in value.items()}
                    rates.provided.add(name)
                continue
            try:
                setattr(rates, name, float(value))
            except (TypeError, ValueError):
                continue
            rates.provided.add(name)
        return rates

    def price_per_kg(self, material: Optional[str]) -> float:
        """What this material costs, falling back to the general figure."""
        if material:
            found = self.filament_prices.get(str(material).upper())
            if found is not None:
                return float(found)
        return float(self.filament_lei_per_kg)


def _lei(value: float) -> float:
    return round(float(value), 2)


def quote(grams: Optional[float],
          seconds: Optional[float],
          rates: Optional[Rates] = None,
          material: Optional[str] = None) -> Dict[str, Any]:
    """What one delivered part costs and what to sell it for.

    `grams` and `seconds` are what the sliced file already recorded -- see
    `slice_info.read_slice_info`. Either being absent is reported rather than
    guessed at: a quote built on an invented print time is exactly the defect
    this module was written to end.
    """
    rates = rates or Rates()

    missing = [name for name, value in (("weight", grams), ("print time", seconds))
               if not value or float(value) <= 0]
    if missing:
        need = " and ".join(missing)
        return {
            "ok": False,
            "currency": rates.currency,
            "missing": missing,
            "message": f"No {need} for this model, so any price would be invented.",
            "guidance": (
                "A .3mf saved by a slicer carries both. A raw .stl carries "
                "neither -- open it in the slicer once and save it as .3mf, or "
                "pass the weight in grams and the print time in hours."),
        }

    # The defaults are Romanian prices in lei. In any other currency they are
    # wrong by an exchange rate, and a quote mixing the user's figures with
    # them would be in no currency at all -- so their own money figures come
    # first.
    if rates.currency.strip().lower() not in ("lei", "ron"):
        needed = [label for label, fields in (
            ("the filament price", ("filament_lei_per_kg", "filament_prices")),
            ("the electricity price", ("electricity_lei_per_kwh",)),
            ("what the printer cost", ("printer_price_lei",)),
            ("what their own hour is worth", ("labour_lei_per_hour",)),
        ) if not any(f in rates.provided for f in fields)]
        if needed:
            return {
                "ok": False,
                "currency": rates.currency,
                "needs": needed,
                "message": (f"To price in {rates.currency} I need "
                            + ", ".join(needed) + "."),
                "guidance": ("Ask the user for those figures in their currency, "
                             "save them with a3d_rates, then quote again."),
            }

    grams = float(grams)
    hours = float(seconds) / 3600.0

    per_kg = rates.price_per_kg(material)
    material_cost = (grams / 1000.0) * per_kg
    electricity_cost = hours * (rates.printer_watts / 1000.0) \
        * rates.electricity_lei_per_kwh

    wear_per_hour = (rates.printer_price_lei / rates.printer_lifetime_hours
                     if rates.printer_lifetime_hours > 0 else 0.0)
    wear_cost = hours * wear_per_hour
    labour_cost = (rates.labour_minutes / 60.0) * rates.labour_lei_per_hour

    direct = material_cost + electricity_cost + wear_cost + labour_cost

    # One delivered part costs more than one attempt, because some attempts do
    # not deliver. Capped below 1.0: a 100% failure rate has no finite cost.
    failure_rate = min(max(float(rates.failure_rate), 0.0), 0.95)
    expected = direct / (1.0 - failure_rate)
    failure_allowance = expected - direct

    margin = min(max(float(rates.margin_percent), 0.0), 99.0)
    price = expected / (1.0 - margin / 100.0)

    def _source(*fields: str) -> str:
        return "yours" if all(f in rates.provided for f in fields) else "assumed"

    cur = rates.currency
    return {
        "ok": True,
        "currency": cur,
        "grams": round(grams, 1),
        "hours": round(hours, 2),
        "material": material or None,
        "breakdown": [
            {"what": "filament", "amount": _lei(material_cost),
             "how": f"{grams:.0f} g at {per_kg:.0f} {cur}/kg",
             "source": _source("filament_lei_per_kg")},
            {"what": "electricity", "amount": _lei(electricity_cost),
             "how": f"{hours:.1f} h at {rates.printer_watts:.0f} W, "
                    f"{rates.electricity_lei_per_kwh:.2f} {cur}/kWh",
             "source": _source("electricity_lei_per_kwh", "printer_watts")},
            {"what": "machine wear", "amount": _lei(wear_cost),
             "how": f"{hours:.1f} h at {wear_per_hour:.2f} {cur}/h "
                    f"({rates.printer_price_lei:.0f} {cur} over "
                    f"{rates.printer_lifetime_hours:.0f} h)",
             "source": _source("printer_price_lei", "printer_lifetime_hours")},
            {"what": "your time", "amount": _lei(labour_cost),
             "how": f"{rates.labour_minutes:.0f} min at "
                    f"{rates.labour_lei_per_hour:.0f} {cur}/h",
             "source": _source("labour_minutes", "labour_lei_per_hour")},
            {"what": "failed prints", "amount": _lei(failure_allowance),
             "how": f"{failure_rate * 100:.0f}% of runs do not deliver",
             "source": _source("failure_rate")},
        ],
        "cost": _lei(expected),
        "break_even": _lei(expected),
        "margin_percent": margin,
        "price": _lei(price),
        "profit": _lei(price - expected),
        "assumed": sorted({item["what"] for item in [
            {"what": "electricity", "ok": "electricity_lei_per_kwh" in rates.provided},
            {"what": "filament price", "ok": "filament_lei_per_kg" in rates.provided},
            {"what": "printer wear", "ok": "printer_price_lei" in rates.provided},
            {"what": "your time", "ok": "labour_lei_per_hour" in rates.provided},
            {"what": "failure rate", "ok": "failure_rate" in rates.provided},
        ] if not item["ok"]}),
    }


def say(result: Dict[str, Any]) -> str:
    """One sentence for the small model to read out.

    Names the price, the floor under it, and which figures were assumed -- a
    quote whose assumptions are invisible gets repeated to a customer as fact.
    """
    if not result.get("ok"):
        return str(result.get("message") or "No price could be worked out.")

    cur = result.get("currency") or ""
    said = (f"Charge about {result['price']:.0f} {cur}. "
            f"It costs you {result['cost']:.0f} {cur} to deliver, so that is "
            f"{result['profit']:.0f} {cur} profit at "
            f"{result['margin_percent']:.0f}% margin.")
    assumed = result.get("assumed") or []
    if assumed:
        said += (" Assumed rather than measured: " + ", ".join(assumed)
                 + " — say the real figure and it will be used from then on.")
    return said
