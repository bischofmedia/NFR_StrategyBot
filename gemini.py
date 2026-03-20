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
Deine Aufgabe: Wähle die zwei schnellsten Strategien für Pole-Start und die zwei schnellsten für Nicht-Pole-Start aus. Es müssen nicht zwingend 1-Stopp und 2-Stopp sein – einfach die zwei besten.
Begründe kurz warum – berücksichtige Reifenabbau, Tankgewicht und Pitstop-Verluste. Hinweis: Es gibt keine Safety Cars in GT7-Rennen.

WICHTIG: Wähle nur Strategien die exakt in den Listen "valid_strategies_from_pole" bzw.
"valid_strategies_not_from_pole" enthalten sind. Erfinde keine neuen Stints.

RENNDATEN:
{json.dumps(data, ensure_ascii=False, indent=2)}

Antworte AUSSCHLIESSLICH mit folgendem JSON – kein Text davor oder danach, keine Markdown-Backticks:
{{
  "strategies": {{
    "pole_1": {{
      "stints": [{{"tyre": "Soft", "laps": 12}}, {{"tyre": "Medium", "laps": 18}}],
      "reasoning": "Kurze Begründung auf Deutsch (1-2 Sätze)"
    }},
    "pole_2": {{
      "stints": [{{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}],
      "reasoning": "..."
    }},
    "no_pole_1": {{
      "stints": [{{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}],
      "reasoning": "..."
    }},
    "no_pole_2": {{
      "stints": [{{"tyre": "...", "laps": 0}}, {{"tyre": "...", "laps": 0}}],
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

    def ensure_stop_coverage(results):
        """Stellt sicher dass mindestens 1-Stopp und 2-Stopp in der Liste sind."""
        stops_present = {r.pit_stops for r in results}
        all_by_stops = {}
        # Sammle alle Ergebnisse aus dem vollen Pool (wird von calculate_strategies zurückgegeben)
        for r in results:
            if r.pit_stops not in all_by_stops:
                all_by_stops[r.pit_stops] = r
        # Füge fehlende hinzu
        extra = [r for stops, r in all_by_stops.items() if stops not in stops_present]
        return results + extra

    all_results_pole    = ensure_stop_coverage(all_results_pole)
    all_results_no_pole = ensure_stop_coverage(all_results_no_pole)

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
            "Pole – Variante 1":       ("pole_1",    all_results_pole),
            "Pole – Variante 2":       ("pole_2",    all_results_pole),
            "Nicht-Pole – Variante 1": ("no_pole_1", all_results_no_pole),
            "Nicht-Pole – Variante 2": ("no_pole_2", all_results_no_pole),
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
    """Algorithmischer Fallback: Top-2 je Pole/Nicht-Pole."""

    def fmt(s):
        if s < 60: return f"{s:.1f}s"
        return f"{int(s//60)}:{s%60:04.1f}min"

    def top2(results):
        seen_desc = set()
        out = []
        for r in sorted(results, key=lambda x: x.total_time_s):
            if r.description not in seen_desc:
                seen_desc.add(r.description)
                out.append(r)
            if len(out) == 2:
                break
        return out

    pole_top    = top2(all_results_pole)
    nopole_top  = top2(all_results_no_pole)

    labels = [
        "Pole – Variante 1", "Pole – Variante 2",
        "Nicht-Pole – Variante 1", "Nicht-Pole – Variante 2",
    ]
    pools = [pole_top, pole_top, nopole_top, nopole_top]
    idxs  = [0, 1, 0, 1]

    strategies = {}
    reasonings = {}

    for label, pool, idx in zip(labels, pools, idxs):
        r = pool[idx] if idx < len(pool) else None
        strategies[label] = r
        if r:
            stops_str = f"{r.pit_stops} Stopp{'s' if r.pit_stops != 1 else ''}"
            reasonings[label] = f"{stops_str}: {r.description}"

    # Gesamtempfehlung
    p1 = strategies.get("Pole – Variante 1")
    p2 = strategies.get("Pole – Variante 2")
    overall = ""
    if p1:
        overall = f"Schnellste Option von der Pole: {p1.description}."
        if p2:
            diff = p2.total_time_s - p1.total_time_s
            overall += f" Variante 2 ({p2.description}) ist {fmt(diff)} langsamer."

    return {"strategies": strategies, "reasonings": reasonings, "overall": overall}

