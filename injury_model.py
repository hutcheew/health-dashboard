import numpy as np
from dataclasses import dataclass
from typing import List, Optional
from collections import deque
from datetime import date as date_cls
import math


# =========================
# CONFIG
# =========================

ACTIVE = "ACTIVE_INJURY"
RECOVERING = "RECOVERING"
RESOLVED = "RESOLVED"
DECONDITIONED = "DECONDITIONED"

STATE_FACTOR = {
    ACTIVE: 1.0,
    RECOVERING: 0.55,
    RESOLVED: 0.2,
    DECONDITIONED: 0.35,
}


@dataclass
class DailyEntry:
    date: str
    stiffness: float
    post_run_pain: float
    load: float = 1.0


class InjuryModel:
    def __init__(self, base_severity: float = 15, injury_date: Optional[date_cls] = None):
        self.base_severity = base_severity
        self.injury_date = injury_date  # real calendar anchor for decay, NOT buffer length

        self.history: deque = deque(maxlen=60)
        # Parallel deque of load_response computed AT INSERTION TIME, using
        # the EMA as it existed before that day's value was folded in --
        # avoids both the "today informs its own baseline" leak and the
        # "recompute whole history off today's final EMA" leak from the
        # original version.
        self.load_response_history: deque = deque(maxlen=60)
        # Cold-start default: ACTIVE, not RESOLVED. update_state() needs a
        # few days of history before it'll classify anything and returns
        # self.state unchanged in the meantime -- defaulting to RESOLVED
        # meant a brand-new injury read as "all clear" for its first few
        # days, which is backwards. Assume injured until shown otherwise.
        self.state = ACTIVE

        self._ema_easy = None
        self._ema_hard = None

    # ---- feature engineering ----

    def symptom_score(self, entry: DailyEntry) -> float:
        return 0.6 * entry.post_run_pain + 0.4 * entry.stiffness

    def rolling(self, values: List[float], window: int) -> float:
        return np.mean(values[-window:]) if len(values) >= window else np.mean(values)

    def rolling_std(self, values: List[float], window: int) -> float:
        return np.std(values[-window:]) if len(values) >= window else np.std(values)

    def ema(self, prev: Optional[float], value: float, alpha: float = 0.2) -> float:
        return value if prev is None else alpha * value + (1 - alpha) * prev

    # ---- load response model ----
    # FIXED ORDER: compute response against the EMA as it stood BEFORE
    # today, then update the EMA with today's value. Same logic as before,
    # just sequenced correctly so today doesn't leak into its own baseline.

    def load_response(self, symptom: float, load: float) -> float:
        expected = self._ema_easy if load <= 1.2 else self._ema_hard
        return 0.0 if expected is None else symptom - expected

    def update_load_model(self, symptom: float, load: float):
        if load <= 1.2:
            self._ema_easy = self.ema(self._ema_easy, symptom)
        else:
            self._ema_hard = self.ema(self._ema_hard, symptom)

    # ---- state machine (unchanged - this part wasn't buggy) ----

    def update_state(self, symptoms: List[float]) -> str:
        if len(symptoms) < 3:
            return self.state
        avg7 = self.rolling(symptoms, 7)
        avg14 = self.rolling(symptoms, 14)
        std14 = self.rolling_std(symptoms, 14)
        trend = np.polyfit(range(len(symptoms[-14:])), symptoms[-14:], 1)[0]
        latest = symptoms[-1]
        flare = latest >= 6 and latest > np.mean(symptoms[-7:]) + 2

        if avg7 >= 4.0 or flare or trend > 0.2:
            return ACTIVE
        if avg14 < 1.5 and std14 < 0.8 and trend <= 1e-6:
            # was trend <= 0 -- np.polyfit on near-constant input returns
            # floating-point noise like 5e-18 or -2e-17, not a clean 0.0.
            # A strict <=0 comparison treats that noise as a meaningful
            # positive trend roughly half the time depending on window
            # length, causing spurious RESOLVED<->RECOVERING flicker on
            # input that never actually changed. 1e-6 is many orders of
            # magnitude below any real symptom trend (units are pain/
            # stiffness points per day) but well above float noise.
            return RESOLVED
        if avg7 < 2.0 and std14 > 1.2:
            return DECONDITIONED
        return RECOVERING

    # ---- instability / recovery ----

    def instability_factor(self, symptoms: List[float]) -> float:
        # Was a "stability_bonus" that DIVIDED the penalty down when symptom
        # variance was high -- meaning erratic, unpredictable pain reduced
        # caution instead of increasing it. Flipped: now multiplies the
        # penalty UP with volatility. An Achilles whose symptoms are
        # swinging around is a worse sign than one that's flat (even flat
        # at a moderate level), and the score should reflect that.
        std14 = self.rolling_std(symptoms, 14)
        return 1 + std14

    def biological_decay(self, days: int) -> float:
        # days must be REAL calendar days since injury onset. The original
        # bug used len(self.history), which caps at deque maxlen (60) and
        # never decays past exp(-60/90)=~0.51 again -- this fix takes days
        # as an explicit calendar-based argument from the caller instead.
        return math.exp(-days / 90)

    def load_adaptation(self, load_responses: List[float]) -> float:
        # FIXED SIGN: original returned 1 - sigmoid(avg), which drives the
        # penalty toward ZERO exactly when symptoms are running worse than
        # expected for the effort -- the opposite of correct. This version
        # is centered at 1.0 (neutral, avg=0) and ranges (0.5, 1.5): worse-
        # than-expected response amplifies the penalty, better-than-expected
        # dampens it.
        if len(load_responses) < 5:
            return 1.0
        avg = np.mean(load_responses[-10:])
        return 0.5 + (1 / (1 + math.exp(-avg)))

    # ---- main update ----

    def update(self, entry: DailyEntry):
        symptom = self.symptom_score(entry)

        # Compute this day's load_response BEFORE updating the EMA with
        # today's value (fixes the leakage), then store it permanently
        # rather than recomputing the whole history retroactively.
        response = self.load_response(symptom, entry.load)
        self.update_load_model(symptom, entry.load)

        self.history.append(entry)
        self.load_response_history.append(response)

        symptoms = [self.symptom_score(e) for e in self.history]
        new_state = self.update_state(symptoms)

        if self.state == RESOLVED and new_state == ACTIVE:
            self.state = ACTIVE
        elif self.state == ACTIVE and new_state == RESOLVED:
            self.state = RECOVERING
        else:
            self.state = new_state

        # Real calendar days since onset, not buffer length
        entry_date = date_cls.fromisoformat(entry.date)
        days = (entry_date - self.injury_date).days if self.injury_date else len(self.history)
        days = max(days, 0)

        # Exposed as its own output (load_tolerance), not just baked silently
        # into the penalty multiplication -- lets the dashboard show WHY the
        # number moved (e.g. "penalty rose because load tolerance dropped",
        # not just "penalty rose"), and makes it independently checkable
        # rather than disconnected dead weight like in the rejected version.
        load_tolerance = self.load_adaptation(list(self.load_response_history))

        penalty = (
            self.base_severity
            * STATE_FACTOR[self.state]
            * self.biological_decay(days)
            * self.instability_factor(symptoms)
            * load_tolerance
        )
        # Defensive ceiling: severe+volatile combinations can compound past
        # the old flat +15/+20 range (legitimately -- that combination is
        # genuinely worse), but no single factor should be able to dominate
        # compute_achilles_score's overall 0-100 clamp on its own.
        penalty = min(penalty, 45)

        return {
            "state": self.state,
            "symptom_score": symptom,
            "avg7": np.mean(symptoms[-7:]),
            "avg14": np.mean(symptoms[-14:]) if len(symptoms) >= 14 else np.mean(symptoms),
            "days_since_onset": days,
            "load_tolerance": float(load_tolerance),
            "injury_penalty": float(penalty),
        }


# =========================
# STATELESS INTEGRATION
# =========================
# health_dashboard.py runs fresh each invocation and exits -- this class has
# no save/load, so plugging it in directly would lose all state every run.
# Matching the as_of-parameterized, stateless pattern the rest of the
# codebase already uses (and backfill_history.py's "replay forward" shape):
# rebuild the model from score_history.json's checkin_stiffness /
# checkin_post_run_pain fields each time, up to the date being scored.
# Cheap (<=60 entries) and means there's nothing extra to persist.

def replay_injury_penalty(score_history: List[dict], as_of: date_cls, injury_date: date_cls,
                           base_severity: float = 15) -> dict:
    """Rebuild InjuryModel state from score_history.json checkin fields,
    up to and including as_of, and return that day's penalty breakdown.
    Days with no checkin data are skipped (not fed as zeros), since a
    missing check-in isn't evidence of zero symptoms.
    """
    model = InjuryModel(base_severity=base_severity, injury_date=injury_date)
    relevant = sorted(
        (h for h in score_history if h.get("date") and h["date"] <= as_of.isoformat()),
        key=lambda h: h["date"],
    )
    result = {"state": ACTIVE, "load_tolerance": 1.0, "injury_penalty": float(base_severity)}
    for h in relevant:
        stiff = h.get("checkin_stiffness")
        pain = h.get("checkin_post_run_pain")
        if stiff is None and pain is None:
            continue
        entry = DailyEntry(
            date=h["date"],
            stiffness=stiff or 0,
            post_run_pain=pain or 0,
            load=1.0,
        )
        result = model.update(entry)
    return result
