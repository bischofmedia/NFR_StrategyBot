import os
import json
from google import genai
from google.genai import types
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
            "fuel_weight_effect_s": f"{fuel_weight_s}s per full tank (linear to 0 at empty). IMPORTANT: Refueling only covers remaining laps needed, NOT full tank.",
            "pit_loss_s": pit_loss,
        },
        "valid_strategies_from_pole": serialize(all_results_pole),
        "valid_strategies_not_from_pole": serialize(all_results_no_pole),
    }

    prompt = f"""Du bist ein erfahrener Simracing-Stratege für Gran Turismo 7.

Dir werden alle vorberechneten validen Rennstrategien übergeben, sortiert nach Gesamtzeit.
Deine Aufgabe: Wähle die BESTE 1-Stopp- und 2-Stopp-Strategie für Pole und Nicht-Pole aus.
Begründe kurz warum – berücksichtige Reifenabbau, Tankgewicht und Pitstop-Verluste. Hinweis: Es gibt keine Safety Cars in GT7-Rennen.

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
    target = tuple((s["tyre"], s["laps"]) for s in stints_json)
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
    # Guard: keine leeren Listen übergeben
    if not all_results_pole or not all_results_no_pole:
        print("[Gemini] Keine validen Strategien – Fallback aktiv")
        return None

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        client = genai.Client(api_key=api_key)

        prompt = _build_prompt(
            all_results_pole, all_results_no_pole,
            track, version, car, total_laps,
            base_soft_s, medium_pct, hard_pct,
            max_soft_runden, reichweite, tank_size,
            start_fuel_pct, pit_loss, fuel_weight_s, hard_enabled,
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )

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
                other = all_results_no_pole if primary_pool is all_results_pole else all_results_pole
                match = _find_matching_result(other, s["stints"])
            result["strategies"][label] = match
            result["reasonings"][label] = s.get("reasoning", "")

        return result

    except Exception as e:
        err = str(e).lower()
        if any(x in err for x in ["quota", "resource_exhausted", "rate", "429", "limit"]):
            print(f"[Gemini] Tageskontingent erschöpft – Fallback aktiv: {e}")
        else:
            import traceback
            print(f"[Gemini] Fehler – Fallback aktiv: {e}")
            traceback.print_exc()
        return None


def fallback_strategies(all_results_pole: list, all_results_no_pole: list) -> dict:
    """Algorithmischer Fallback wenn Gemini nicht verfügbar."""

    def best_by_stops(results, stops):
        return next((r for r in results if r.pit_stops == stops), None)

    def fmt(s):
        if s < 60:
            return f"{s:.1f}s"
        mins = int(s // 60); secs = s % 60
        return f"{mins}:{secs:04.1f}min"

    strategies = {
        "1-Stopp Pole":       best_by_stops(all_results_pole,    1),
        "1-Stopp Nicht-Pole": best_by_stops(all_results_no_pole, 1),
        "2-Stopp Pole":       best_by_stops(all_results_pole,    2),
        "2-Stopp Nicht-Pole": best_by_stops(all_results_no_pole, 2),
    }

    reasonings = {}
    s1p = strategies.get("1-Stopp Pole")
    s2p = strategies.get("2-Stopp Pole")
    s1n = strategies.get("1-Stopp Nicht-Pole")
    s2n = strategies.get("2-Stopp Nicht-Pole")

    # Vergleich 1-Stopp vs 2-Stopp immer einbauen
    diff_1v2 = fmt(abs(s1p.total_time_s - s2p.total_time_s)) if s1p and s2p else None
    faster   = ("1-Stopp" if s1p.total_time_s <= s2p.total_time_s else "2-Stopp") if s1p and s2p else None

    if s1p:
        soft_r   = sum(n for t, n in s1p.stints if t == TYRE_SOFT)
        diff_str = f" – {diff_1v2} schneller als 2-Stopp" if diff_1v2 and faster == "1-Stopp" else                    f" – {diff_1v2} langsamer als 2-Stopp" if diff_1v2 else ""
        reasonings["1-Stopp Pole"] = f"Minimaler Boxenstopp-Verlust, {soft_r} Soft-Runden{diff_str}."

    if s2p:
        diff_str = f" – {diff_1v2} schneller als 1-Stopp" if diff_1v2 and faster == "2-Stopp" else                    f" – {diff_1v2} langsamer als 1-Stopp" if diff_1v2 else ""
        reasonings["2-Stopp Pole"] = f"Frischere Reifen durch zweiten Stopp{diff_str}."

    if s1n:
        reasonings["1-Stopp Nicht-Pole"] = "Verkehrsmalus R1+2s/R2+1.5s/R3+1s kostet etwas Zeit gegenüber Pole."

    if s2n:
        diff_str = f" – {fmt(abs(s2n.total_time_s - s1n.total_time_s))} langsamer als 1-Stopp" if s1n else ""
        reasonings["2-Stopp Nicht-Pole"] = f"Zwei Stopps bei Nicht-Pole-Start{diff_str}."

    overall = ""
    if s1p and s2p:
        diff = abs(s1p.total_time_s - s2p.total_time_s)
        overall = (
            f"**{faster}** ist {fmt(diff)} schneller. "
            + ("Der Pitstop-Verlust überwiegt den Vorteil frischer Reifen."
               if faster == "1-Stopp"
               else "Frische Reifen im zweiten Stint gleichen den Pitstop-Verlust mehr als aus.")
        )
    elif s1p:
        overall = "Nur 1-Stopp-Strategie verfügbar."
    elif s2p:
        overall = "Nur 2-Stopp-Strategie verfügbar."

    return {"strategies": strategies, "reasonings": reasonings, "overall": overall}

