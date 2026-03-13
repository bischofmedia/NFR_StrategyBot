import os
import json
import google.generativeai as genai
from strategy import StrategyResult, TYRE_SOFT, TYRE_MEDIUM, TYRE_HARD


def _build_prompt(
    all_results_pole: list,
    all_results_no_pole: list,
    track: str, version: str, car: str, total_laps: int,
    base_soft_s: float, medium_pct: float, hard_pct: float,
    max_soft_runden: int, reichweite: int, tank_size: float,
    start_fuel_pct: float, pit_loss: float, fuel_weight_s: float,
    hard_enabled: bool,
) -> str:

    def fmt(s):
        mins = int(s // 60)
        secs = s % 60
        return f"{mins}:{secs:06.3f}"

    def serialize(results):
        out = []
        for r in results[:20]:
            out.append({
                "stints": [{"tyre": t, "laps": n} for t, n in r.stints],
                "total_time": fmt(r.total_time_s),
                "total_time_s": round(r.total_time_s, 3),
                "pit_stops": r.pit_stops,
                "fuel_stops_after_stint": [s + 1 for s in r.fuel_stops],
            })
        return out

    data = {
        "race": {
            "track": f"{track}{' – ' + version if version else ''}",
            "car": car,
            "total_laps": total_laps,
        },
        "tyre_data": {
            "soft_base_time": fmt(base_soft_s),
            "medium_pct_slower": round(medium_pct, 3),
            "hard_pct_slower": round(hard_pct, 3) if hard_enabled else "disabled",
            "max_soft_laps": max_soft_runden,
            "soft_degradation": "Lap 1: +0.5s (cold tyres). Laps 2 to 40%: plateau (optimal). 40-70%: +0 to +1s. 70-100%: +1 to +3s.",
        },
        "fuel_data": {
            "tank_size_l": tank_size,
            "start_fuel_pct": start_fuel_pct,
            "start_fuel_l": round(tank_size * start_fuel_pct / 100, 1),
            "laps_on_start_fuel": reichweite,
            "fuel_weight_effect_s": f"{fuel_weight_s}s per full tank, linear decrease to 0 at empty",
            "pit_loss_s": pit_loss,
        },
        "valid_strategies_from_pole": serialize(all_results_pole),
        "valid_strategies_not_from_pole": serialize(all_results_no_pole),
    }

    prompt = f"""Du bist ein erfahrener Simracing-Stratege für Gran Turismo 7.

Dir werden alle vorberechneten validen Rennstrategien übergeben, sortiert nach Gesamtzeit.
Deine Aufgabe: Wähle die BESTE 1-Stopp- und 2-Stopp-Strategie für Pole und Nicht-Pole aus.
Begründe kurz warum – berücksichtige Reifenabbau, Tankgewicht, Pitstop-Verluste und Taktik.

WICHTIG: Wähle nur Strategien die exakt in den Listen "valid_strategies_from_pole" bzw.
"valid_strategies_not_from_pole" enthalten sind. Erfinde keine neuen Stints.

RENNDATEN:
{json.dumps(data, ensure_ascii=False, indent=2)}

Antworte AUSSCHLIESSLICH mit folgendem JSON – kein Text davor oder danach, keine Markdown-Backticks:
{{
  "strategies": {{
    "1_stop_pole": {{
      "stints": [{{"tyre": "Soft", "laps": 9}}, {{"tyre": "Medium", "laps": 21}}],
      "reasoning": "Kurze Begründung auf Deutsch (1-2 Sätze)"
    }},
    "1_stop_no_pole": {{
      "stints": [{{"tyre": "...", "laps": 0}}],
      "reasoning": "..."
    }},
    "2_stop_pole": {{
      "stints": [{{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}],
      "reasoning": "..."
    }},
    "2_stop_no_pole": {{
      "stints": [{{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}],
      "reasoning": "..."
    }}
  }},
  "overall_recommendation": "2-3 Sätze auf Deutsch: wann welche Strategie optimal ist und warum"
}}"""
    return prompt


def _find_matching_result(results: list, stints_json: list) -> StrategyResult | None:
    """Findet das passende StrategyResult anhand der Stint-Struktur aus dem Gemini-JSON."""
    target = tuple((s["tyre"], s["laps"]) for s in stints_json)
    # Exakter Match zuerst
    for r in results:
        if tuple(r.stints) == target:
            return r
    # Fallback: gleiche Tyre-Gesamtrunden + gleiche Stopp-Anzahl
    target_totals = {}
    for s in stints_json:
        target_totals[s["tyre"]] = target_totals.get(s["tyre"], 0) + s["laps"]
    target_stops = len(stints_json) - 1
    for r in results:
        if r.pit_stops != target_stops:
            continue
        r_totals = {}
        for t, n in r.stints:
            r_totals[t] = r_totals.get(t, 0) + n
        if r_totals == target_totals:
            return r
    return None


def get_gemini_strategies(
    all_results_pole: list,
    all_results_no_pole: list,
    track: str, version: str, car: str, total_laps: int,
    base_soft_s: float, medium_pct: float, hard_pct: float,
    max_soft_runden: int, reichweite: int, tank_size: float,
    start_fuel_pct: float, pit_loss: float, fuel_weight_s: float,
    hard_enabled: bool,
) -> dict | None:
    """
    Lässt Gemini 2.5 Flash die besten 4 Strategien auswählen und begründen.
    Temperature=0 für deterministische, reproduzierbare Ausgabe.
    Gibt None zurück bei Fehler oder erschöpftem Tageskontingent → Fallback greift.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            model_name="gemini-2.5-flash",
            generation_config=genai.GenerationConfig(
                temperature=0,
                response_mime_type="application/json",
            )
        )

        prompt = _build_prompt(
            all_results_pole, all_results_no_pole,
            track, version, car, total_laps,
            base_soft_s, medium_pct, hard_pct,
            max_soft_runden, reichweite, tank_size,
            start_fuel_pct, pit_loss, fuel_weight_s, hard_enabled,
        )

        response = model.generate_content(prompt)
        raw = response.text.strip()
        # Sicherheits-Strip falls doch Backticks kommen
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        parsed  = json.loads(raw)
        strats  = parsed.get("strategies", {})
        overall = parsed.get("overall_recommendation", "")

        key_map = {
            "1-Stopp Pole":       ("1_stop_pole",    all_results_pole),
            "1-Stopp Nicht-Pole": ("1_stop_no_pole", all_results_no_pole),
            "2-Stopp Pole":       ("2_stop_pole",    all_results_pole),
            "2-Stopp Nicht-Pole": ("2_stop_no_pole", all_results_no_pole),
        }

        result = {"overall": overall, "strategies": {}, "reasonings": {}}

        for label, (json_key, primary_pool) in key_map.items():
            s = strats.get(json_key)
            if not s:
                continue
            match = _find_matching_result(primary_pool, s["stints"])
            if match is None:
                # Fallback: im anderen Pool suchen
                other = all_results_no_pole if primary_pool is all_results_pole else all_results_pole
                match = _find_matching_result(other, s["stints"])
            result["strategies"][label] = match
            result["reasonings"][label] = s.get("reasoning", "")

        return result

    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["quota", "resource_exhausted", "rate", "429", "limit"]):
            print(f"[Gemini] Tageskontingent erschöpft – Fallback aktiv")
        else:
            print(f"[Gemini] Fehler – Fallback aktiv: {e}")
        return None


def fallback_strategies(all_results_pole: list, all_results_no_pole: list) -> dict:
    """Algorithmischer Fallback wenn Gemini nicht verfügbar."""

    def best_by_stops(results, stops):
        return next((r for r in results if r.pit_stops == stops), None)

    def fmt(s):
        mins = int(s // 60); secs = s % 60
        return f"{mins}:{secs:05.1f}"

    strategies = {
        "1-Stopp Pole":       best_by_stops(all_results_pole,    1),
        "1-Stopp Nicht-Pole": best_by_stops(all_results_no_pole, 1),
        "2-Stopp Pole":       best_by_stops(all_results_pole,    2),
        "2-Stopp Nicht-Pole": best_by_stops(all_results_no_pole, 2),
    }

    reasonings = {}
    s1p = strategies["1-Stopp Pole"]
    s2p = strategies["2-Stopp Pole"]

    if s1p:
        soft_r = sum(n for t, n in s1p.stints if t == TYRE_SOFT)
        reasonings["1-Stopp Pole"] = (
            f"Minimaler Pitstop-Verlust. {soft_r} Soft-Runden nutzen das Plateau optimal."
        )
    if strategies["1-Stopp Nicht-Pole"]:
        reasonings["1-Stopp Nicht-Pole"] = (
            "Gleiche Strategie wie von der Pole – Verkehr in den ersten Runden kostet etwas Zeit."
        )
    if s2p and s1p:
        diff = s2p.total_time_s - s1p.total_time_s
        reasonings["2-Stopp Pole"] = (
            f"Frischere Reifen in der zweiten Hälfte, aber {fmt(diff)} langsamer als 1-Stopp."
        )
    if strategies["2-Stopp Nicht-Pole"]:
        reasonings["2-Stopp Nicht-Pole"] = (
            "Mehr Flexibilität bei schlechtem Startplatz – zweiter Stopp ermöglicht Undercut."
        )

    overall = ""
    if s1p and s2p:
        diff = s2p.total_time_s - s1p.total_time_s
        overall = (
            f"Die 1-Stopp-Strategie ist {fmt(diff)} schneller als 2 Stopps. "
            f"Von der Pole klar zu bevorzugen. "
            f"Die 2-Stopp-Variante lohnt sich taktisch nur bei Safety-Car oder Undercut-Möglichkeit."
        ) if diff > 10 else (
            f"1- und 2-Stopp liegen nur {fmt(diff)} auseinander – "
            f"bei Safety-Car kann ein zweiter Stopp taktisch entscheidend sein."
        )

    return {"strategies": strategies, "reasonings": reasonings, "overall": overall}
