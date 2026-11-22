"""
Imitation of typing by a live human: uneven pace, pauses on punctuation and "thinking"
pauses, typos (neighbouring key, doubled letter, swapped letters, skipped letter,
extra letter) and their correction with Backspace.

The module knows nothing about the keyboard: it is given send_char / send_backspace / wait
functions. That is why it can be tested without any real input.
"""
import math
import random
from typing import Callable, Optional

DEFAULTS = {
    "cpm": 800,               # nominal pace, characters per minute (about 500 in reality, with pauses and typos)
    "typo_rate": 0.035,       # probability of a typo on every letter
    "think_chance": 0.02,     # probability of a "thinking" pause between words
    "start_delay": [0.4, 0.9],  # pause before the first character, seconds
}

# Keyboard layouts for neighbouring keys (a typo = pressing a neighbouring key).
# Latin and Cyrillic (Russian) layouts are included.
_ROWS = (
    ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
    ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю"),
)


def _build_neighbors() -> dict:
    nb: dict = {}
    for rows in _ROWS:
        for r, row in enumerate(rows):
            for i, ch in enumerate(row):
                near = []
                # (row offset, index offsets): the upper row is shifted by half a key
                for dr, offs in ((0, (-1, 1)), (-1, (0, 1)), (1, (-1, 0))):
                    rr = r + dr
                    if 0 <= rr < len(rows):
                        for o in offs:
                            j = i + o
                            if 0 <= j < len(rows[rr]):
                                near.append(rows[rr][j])
                nb[ch] = near
    return nb


_NEIGHBORS = _build_neighbors()


class _Abort(Exception):
    pass


class HumanTyper:
    def __init__(
        self,
        send_char: Callable[[str], None],
        send_backspace: Callable[[], None],
        wait: Callable[[float], bool],
        cfg: Optional[dict] = None,
        rng: Optional[random.Random] = None,
    ):
        """wait(seconds) must return True if the typing has to be aborted."""
        c = {**DEFAULTS, **(cfg or {})}
        self.send_char = send_char
        self.send_backspace = send_backspace
        self.wait = wait
        self.rng = rng or random.Random()
        self.set_cpm(c["cpm"])
        self.typo_rate = float(c["typo_rate"])
        self.think_chance = float(c["think_chance"])
        self.start_delay = tuple(c["start_delay"])
        self.speed = 1.0  # slowly "drifts": speeds up, then slows down

    def set_cpm(self, cpm: float) -> None:
        """Changes the pace on the fly (can be called during typing too)."""
        cpm = max(60.0, float(cpm))
        self.base = 60.0 / cpm
        # at high speeds the pauses (punctuation, "thinking", corrections) are shorter too
        self.ps = min(1.0, 420.0 / cpm)

    # --- internals ---
    def _sleep(self, seconds: float) -> None:
        if self.wait(seconds):
            raise _Abort

    def _neighbor(self, ch: str) -> Optional[str]:
        near = _NEIGHBORS.get(ch.lower())
        if not near:
            return None
        n = self.rng.choice(near)
        return n.upper() if ch.isupper() else n

    def _gap(self, ch: str) -> float:
        """Pause after the character ch until the next key press."""
        r = self.rng
        d = r.lognormvariate(math.log(self.base / self.speed), 0.35)
        d = min(max(d, 0.025), 0.7)
        ps = self.ps
        if ch.isupper():
            d += r.uniform(0.02, 0.08) * ps  # Shift
        if ch == " ":
            d += r.uniform(0.0, 0.05) * ps
            if r.random() < self.think_chance:
                d += r.uniform(0.7, 2.2) * ps
        elif ch in ",;":
            d += r.uniform(0.12, 0.35) * ps
        elif ch in ".!?:…":
            d += r.uniform(0.25, 0.8) * ps
        elif ch == "\n":
            d += r.uniform(0.35, 1.1) * ps
        return d

    def _type(self, ch: str) -> None:
        self.send_char(ch)
        self._sleep(self._gap(ch))

    # --- typos ---
    def _plan_typo(self, text: str, i: int):
        """Returns (what_was_typed_wrong, what_it_should_be, how_many_text_characters_are_covered) or None."""
        r = self.rng
        n = len(text)
        ch = text[i]
        if not ch.isalpha():
            return None

        has_next = i + 1 < n and text[i + 1] != "\n"
        kinds, weights = [], []
        if self._neighbor(ch):
            kinds += ["neighbor", "extra"]
            weights += [50, 8]
        kinds.append("double")
        weights.append(14)
        if has_next and text[i + 1].isalpha() and text[i + 1].lower() != ch.lower():
            kinds.append("swap")
            weights.append(16)
        if has_next:
            kinds.append("skip")
            weights.append(12)
        kind = r.choices(kinds, weights)[0]

        # After how many characters the human noticed the mistake
        if kind == "skip":
            lag = r.choices([1, 2, 3, 4], [30, 30, 25, 15])[0]
        else:
            lag = r.choices([0, 1, 2, 3, 4], [40, 22, 17, 12, 9])[0]

        if kind == "neighbor":
            wrong, used = self._neighbor(ch), 1
        elif kind == "extra":
            nb = self._neighbor(ch)
            wrong, used = (nb + ch) if r.random() < 0.5 else (ch + nb), 1
        elif kind == "double":
            wrong, used = ch + ch, 1
        elif kind == "swap":
            wrong, used = text[i + 1] + ch, 2
        else:  # skip
            wrong, used = "", 1

        tail = []
        j = i + used
        while len(tail) < lag and j < n and text[j] != "\n":
            tail.append(text[j])
            j += 1
        tail_s = "".join(tail)
        if kind == "skip" and not tail_s:
            return None

        typed = wrong + tail_s
        correct = text[i:i + used] + tail_s
        if typed == correct:
            return None
        return typed, correct, used + len(tail_s)

    def _do_typo(self, typed: str, correct: str) -> None:
        r = self.rng
        for c in typed:
            self._type(c)
        # noticed the mistake
        self._sleep(r.uniform(0.18, 0.5) * self.ps)
        # erase only what actually differs
        k = 0
        while k < min(len(typed), len(correct)) and typed[k] == correct[k]:
            k += 1
        for _ in range(len(typed) - k):
            self.send_backspace()
            self._sleep(r.uniform(0.04, 0.11) * max(self.ps, 0.5))
        self._sleep(r.uniform(0.08, 0.25) * self.ps)
        for c in correct[k:]:
            self._type(c)

    # --- main ---
    def type_text(self, text: str) -> bool:
        """Types text. True = finished, False = aborted."""
        r = self.rng
        try:
            self._sleep(r.uniform(*self.start_delay))
            i, n = 0, len(text)
            while i < n:
                self.speed = min(1.3, max(0.7, self.speed + r.gauss(0, 0.05)))
                plan = self._plan_typo(text, i) if r.random() < self.typo_rate else None
                if plan:
                    typed, correct, covered = plan
                    self._do_typo(typed, correct)
                    i += covered
                else:
                    self._type(text[i])
                    i += 1
            return True
        except _Abort:
            return False


# --- fitting a desired real speed ---
def real_speed(text: str, cpm: float, cfg: Optional[dict] = None, seeds=(1, 2, 3)) -> float:
    """Simulation without waiting: how many characters per minute you actually get at the nominal pace cpm
    (pauses and typos included). The nominal cpm is noticeably higher than the real speed."""
    total_time = total_chars = 0.0
    for s in seeds:
        t = [0.0]

        def wait(seconds: float) -> bool:
            t[0] += seconds
            return False

        c = {**(cfg or {}), "cpm": cpm, "start_delay": [0, 0]}
        HumanTyper(lambda ch: None, lambda: None, wait, c, random.Random(s)).type_text(text)
        total_time += t[0]
        total_chars += len(text)
    return total_chars / total_time * 60.0 if total_time > 0 else float(cpm)


def cpm_for_speed(text: str, speed: float, cfg: Optional[dict] = None) -> float:
    """Picks the nominal cpm so that the real typing speed of this text is about `speed` chars/min."""
    sample = text[:800] or "a"
    lo, hi = 60.0, 8000.0
    if real_speed(sample, lo, cfg) >= speed:
        return lo
    if real_speed(sample, hi, cfg) <= speed:
        return hi
    for _ in range(14):  # bisection on a logarithmic scale
        mid = math.sqrt(lo * hi)
        if real_speed(sample, mid, cfg) < speed:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)
