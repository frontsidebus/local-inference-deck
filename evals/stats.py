"""Statistics for eval reports: paired tests, effect sizes with intervals, multiplicity, equivalence and power.

Standard library only (no numpy/scipy), so the report runs anywhere the harness runs. Every resampling routine
takes an explicit seed and uses its own ``random.Random``, so a report is reproducible byte for byte.

Conventions
-----------
- Paired binary outcomes on the same items are summarised by the 2x2 table ``n11`` (both pass), ``n10`` (only A),
  ``n01`` (only B), ``n00`` (neither). The effect is the paired difference in accuracy ``d = (n10 - n01) / n``.
- Continuous paired outcomes (CVSS absolute error, partial credit, per-item mean pass over seeds) are a list of
  per-item differences ``a_i - b_i``.
- Every function returns plain floats, so callers can format them as they like.

Methods
-------
- ``mcnemar_exact``: two-sided exact (binomial) McNemar test on the discordant pairs.
- ``newcombe_paired_ci``: Newcombe (1998) method 10, the hybrid score interval for a difference of paired
  proportions (Wilson intervals for each margin combined with the phi correlation). Good coverage at small n,
  unlike the Wald interval.
- ``holm`` / ``bh``: Holm step-down family-wise error adjustment and Benjamini-Hochberg false-discovery adjustment.
- ``tost_paired_binary``: two one-sided tests for equivalence within +/- margin, done by inverting the Newcombe
  interval (equivalence at level alpha <=> the 100(1-2 alpha)% interval lies inside the margins). The p-value is
  the smallest alpha at which that holds.
- ``tost_paired_wald``: the textbook Wald-statistic TOST for paired proportions (kept for cross-checks).
- ``bootstrap_mean_ci``: percentile bootstrap of a mean of paired differences, optionally resampling whole clusters
  (template duplicates that share a ``cluster_id``), which gives a cluster-robust interval.
- ``sign_flip_p``: paired permutation (sign-flip) test of mean difference 0; exact when there are few non-zero
  differences, Monte Carlo otherwise.
- ``power_mcnemar_exact`` / ``mde_mcnemar_exact``: exact power of the exact McNemar test (sum over the
  discordant count) and the minimum detectable effect from it. ``mde_mcnemar`` (Connor 1987 normal approximation)
  is kept for reference only: it is optimistic (about 0.76-0.79 real power at its "80%" MDE).
- ``power_tost_paired_sim``: simulated power of the Newcombe TOST; ``power_mean_diff`` / ``power_tost_mean``:
  normal-approximation power for paired mean differences (MAE); ``n_for_power``: sample size for a target power.
- ``mde_paired_mean``: z-approximation MDE for a mean difference.
"""
from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist
from typing import Dict, List, Optional, Sequence, Tuple

_N = NormalDist()
DEFAULT_SEED = 20261011
DEFAULT_BOOT = 4000
DEFAULT_PERM = 10000


def z_of(conf: float) -> float:
    """Two-sided normal quantile for a confidence level (0.95 -> 1.96)."""
    return _N.inv_cdf(0.5 + conf / 2)


def wilson(k: int, n: int, z: float = 1.959963984540054) -> Tuple[float, float]:
    """Wilson score interval for k successes in n trials; (0, 1) when n = 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


# ------------------------------------------------------------------------------------------- paired binary
def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value: b and c are the discordant counts (only A passed, only B passed)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def newcombe_paired_ci(n11: int, n10: int, n01: int, n00: int, conf: float = 0.95) -> Tuple[float, float, float]:
    """(difference, lower, upper) for p_A - p_B on paired binary data, Newcombe (1998) method 10."""
    n = n11 + n10 + n01 + n00
    if n == 0:
        return 0.0, -1.0, 1.0
    z = z_of(conf)
    p1, p2 = (n11 + n10) / n, (n11 + n01) / n
    l1, u1 = wilson(n11 + n10, n, z)
    l2, u2 = wilson(n11 + n01, n, z)
    den = (n11 + n10) * (n01 + n00) * (n11 + n01) * (n10 + n00)
    phi = (n11 * n00 - n10 * n01) / math.sqrt(den) if den else 0.0
    d = p1 - p2
    dl = math.sqrt(max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    du = math.sqrt(max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2))
    return d, max(-1.0, d - dl), min(1.0, d + du)


def _bisect(f, lo: float, hi: float, iters: int = 80) -> float:
    """Root of a monotone f on [lo, hi] where f(lo) and f(hi) differ in sign."""
    flo = f(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2
        if (f(mid) > 0) == (flo > 0):
            lo, flo = mid, f(mid)
        else:
            hi = mid
    return (lo + hi) / 2


def tost_paired_binary(n11: int, n10: int, n01: int, n00: int, margin: float) -> Dict[str, float]:
    """Equivalence of paired proportions within +/- margin, by inverting the Newcombe interval.

    p_lower tests H0: d <= -margin; p_upper tests H0: d >= +margin; p = max of the two. The one-sided p-value is the
    alpha at which the one-sided (1 - alpha) Newcombe bound reaches the margin (two-sided level 1 - 2 alpha)."""
    if margin <= 0:
        raise ValueError("margin must be > 0")

    def lower(alpha: float) -> float:
        return newcombe_paired_ci(n11, n10, n01, n00, 1 - 2 * alpha)[1]

    def upper(alpha: float) -> float:
        return newcombe_paired_ci(n11, n10, n01, n00, 1 - 2 * alpha)[2]

    eps, half = 1e-12, 0.5 - 1e-12
    # lower(alpha) rises with alpha (the interval narrows); the p-value is where it crosses -margin
    if lower(half) <= -margin:
        p_lo = 1.0
    elif lower(eps) > -margin:
        p_lo = eps
    else:
        p_lo = _bisect(lambda a: lower(a) + margin, eps, half)
    if upper(half) >= margin:
        p_hi = 1.0
    elif upper(eps) < margin:
        p_hi = eps
    else:
        p_hi = _bisect(lambda a: margin - upper(a), eps, half)
    return {"p": max(p_lo, p_hi), "p_lower": p_lo, "p_upper": p_hi}


def tost_paired_wald(n10: int, n01: int, n: int, margin: float) -> Dict[str, float]:
    """Wald TOST for paired proportions: se = sqrt((psi - d^2) / n), psi = discordant share, d = (n10 - n01) / n."""
    if n <= 0:
        return {"p": 1.0, "p_lower": 1.0, "p_upper": 1.0, "se": float("nan")}
    d, psi = (n10 - n01) / n, (n10 + n01) / n
    se = math.sqrt(max(0.0, psi - d * d) / n)
    if se == 0:
        p_lo = 0.0 if d > -margin else 1.0
        p_hi = 0.0 if d < margin else 1.0
    else:
        p_lo = 1 - _N.cdf((d + margin) / se)
        p_hi = _N.cdf((d - margin) / se)
    return {"p": max(p_lo, p_hi), "p_lower": p_lo, "p_upper": p_hi, "se": se}


def mde_mcnemar(n: int, psi: float, alpha: float = 0.05, power: float = 0.8) -> Optional[float]:
    """Smallest |p_A - p_B| a two-sided McNemar test detects with the given power on n pairs (Connor 1987).

    psi is the discordant share (n10 + n01) / n. A true difference delta forces psi >= delta, so psi is floored at
    delta while solving. Returns None when n = 0, and 1.0 when no difference up to 1 is detectable."""
    if n <= 0:
        return None
    za, zb = _N.inv_cdf(1 - alpha / 2), _N.inv_cdf(power)

    def needed_n(delta: float) -> float:
        ps = max(psi, delta)
        return (za * math.sqrt(ps) + zb * math.sqrt(max(0.0, ps - delta * delta))) ** 2 / (delta * delta)

    if needed_n(1.0) > n:
        return 1.0
    return _bisect(lambda dl: needed_n(dl) - n, 1e-6, 1.0)


def mde_paired_mean(sd: float, n: int, alpha: float = 0.05, power: float = 0.8) -> Optional[float]:
    """Smallest mean paired difference detectable with the given power: (z_{1-a/2} + z_power) * sd / sqrt(n)."""
    if n <= 1:
        return None
    return (_N.inv_cdf(1 - alpha / 2) + _N.inv_cdf(power)) * sd / math.sqrt(n)


# ------------------------------------------------------------------------------------------- exact / simulated power
def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the regularized incomplete beta (modified Lentz, Numerical Recipes 6.4)."""
    tiny, eps = 1e-300, 3e-16
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 10000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def betainc(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbt = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbt) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbt) * _betacf(b, a, 1 - x) / b


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n:
        return 1.0
    if p <= 0:
        return 1.0
    if p >= 1:
        return 0.0
    return betainc(n - k, k + 1, 1 - p)


def _binom_logpmf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1:
        return 0.0 if k == n else -math.inf
    return (math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p)
            + (n - k) * math.log1p(-p))


_CRIT: Dict[Tuple[int, float], int] = {}


def mcnemar_critical(d: int, alpha: float) -> int:
    """Largest k with exact two-sided McNemar p(k, d - k) < alpha (reject when min(b, c) <= k); -1 if none."""
    key = (d, alpha)
    if key in _CRIT:
        return _CRIT[key]
    lo, hi = -1, d // 2          # invariant: lo rejects (or is -1), hi + 1 does not
    if d == 0 or 2 * binom_cdf(0, d, 0.5) >= alpha:
        _CRIT[key] = -1
        return -1
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if min(1.0, 2 * binom_cdf(mid, d, 0.5)) < alpha:
            lo = mid
        else:
            hi = mid - 1
    _CRIT[key] = lo
    return lo


def power_mcnemar_exact(n: int, psi: float, delta: float, alpha: float = 0.05) -> float:
    """Exact power of the two-sided exact McNemar test on n pairs, when the discordant share is psi and the true
    difference p_A - p_B is delta (floored: psi >= |delta|). Sums over the number of discordant pairs D ~ Bin(n, psi)
    and, given D, the A-only count ~ Bin(D, (psi + delta) / (2 psi))."""
    if n <= 0:
        return 0.0
    delta = abs(delta)
    ps = min(1.0, max(psi, delta))
    if ps <= 0:
        return 0.0
    q = min(1.0, (ps + delta) / (2 * ps))
    mu, sdv = n * ps, math.sqrt(max(n * ps * (1 - ps), 1e-12))
    d_lo, d_hi = max(0, int(mu - 12 * sdv) - 1), min(n, int(mu + 12 * sdv) + 1)
    tot = 0.0
    for d in range(d_lo, d_hi + 1):
        w = math.exp(_binom_logpmf(d, n, ps))
        if w < 1e-15:
            continue
        k = mcnemar_critical(d, alpha)
        if k < 0:
            continue
        tot += w * (binom_cdf(k, d, q) + (1 - binom_cdf(d - k - 1, d, q)))
    return min(1.0, tot)


def mde_mcnemar_exact(n: int, psi: float, alpha: float = 0.05, power: float = 0.8) -> Optional[float]:
    """Smallest |p_A - p_B| with exact McNemar power >= ``power`` at the observed discordant share (psi floored at
    delta). None when n = 0; 1.0 when nothing up to 1 is detectable. Replaces the optimistic Connor approximation."""
    if n <= 0:
        return None
    if power_mcnemar_exact(n, psi, 1.0, alpha) < power:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if power_mcnemar_exact(n, psi, mid, alpha) >= power:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-4:
            break
    return hi


def _multinomial4(rng: random.Random, n: int, p11: float, p10: float, p01: float) -> Tuple[int, int, int, int]:
    def binv(k: int, p: float) -> int:
        p = min(1.0, max(0.0, p))
        if k <= 0 or p <= 0:
            return 0
        if p >= 1:
            return k
        return rng.binomialvariate(k, p) if hasattr(rng, "binomialvariate") else sum(rng.random() < p for _ in range(k))
    n10 = binv(n, p10)
    rest = n - n10
    n01 = binv(rest, p01 / (1 - p10)) if p10 < 1 else 0
    rest -= n01
    rem = 1 - p10 - p01
    n11 = binv(rest, p11 / rem) if rem > 0 else 0
    return n11, n10, n01, rest - n11


def paired_cells(p_b: float, psi: float, delta: float) -> Tuple[float, float, float, float]:
    """(p11, p10, p01, p00) for B's accuracy p_b, discordant share psi and true difference delta = p_A - p_B."""
    ps = max(psi, abs(delta))
    p10, p01 = (ps + delta) / 2, (ps - delta) / 2
    p11 = p_b - p01
    p00 = 1 - p11 - p10 - p01
    if min(p11, p00) < -1e-12:
        raise ValueError(f"no 2x2 table has p_B={p_b}, psi={psi}, delta={delta}")
    return max(0.0, p11), p10, p01, max(0.0, p00)


def power_tost_paired_sim(n: int, p_b: float, psi: float, margin: float, delta: float = 0.0,
                          alpha: float = 0.05, n_sim: int = 2000, seed: int = DEFAULT_SEED) -> float:
    """Simulated power of the Newcombe-based TOST (equivalent iff the 100(1-2 alpha)% interval is inside +/- margin)
    when the true difference is delta."""
    if n <= 0:
        return 0.0
    p11, p10, p01, _ = paired_cells(p_b, psi, delta)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n_sim):
        c = _multinomial4(rng, n, p11, p10, p01)
        _, lo, hi = newcombe_paired_ci(*c, 1 - 2 * alpha)
        hits += (-margin < lo and hi < margin)
    return hits / n_sim


def power_mcnemar_sim(n: int, p_b: float, psi: float, delta: float, alpha: float = 0.05, n_sim: int = 4000,
                      seed: int = DEFAULT_SEED) -> float:
    """Simulated power of the exact McNemar test (a cross-check on ``power_mcnemar_exact``)."""
    p11, p10, p01, _ = paired_cells(p_b, psi, delta)
    rng = random.Random(seed)
    return sum(mcnemar_exact(c[1], c[2]) < alpha for c in (_multinomial4(rng, n, p11, p10, p01)
                                                         for _ in range(n_sim))) / n_sim


def power_mean_diff(n: int, sd_diff: float, delta: float, alpha: float = 0.05) -> float:
    """Normal-approximation power of a two-sided test of a paired mean difference (e.g. MAE)."""
    if n <= 1 or sd_diff <= 0:
        return 1.0 if delta else 0.0
    ncp = abs(delta) * math.sqrt(n) / sd_diff
    za = _N.inv_cdf(1 - alpha / 2)
    return _N.cdf(ncp - za) + _N.cdf(-ncp - za)


def power_tost_mean(n: int, sd_diff: float, margin: float, delta: float = 0.0, alpha: float = 0.05) -> float:
    """Normal-approximation power of TOST on a paired mean difference within +/- margin."""
    if n <= 1 or sd_diff <= 0:
        return 1.0 if abs(delta) < margin else 0.0
    se = sd_diff / math.sqrt(n)
    za = _N.inv_cdf(1 - alpha)
    return max(0.0, _N.cdf((margin - delta) / se - za) - _N.cdf((-margin - delta) / se + za))


def n_for_power(power_fn, target: float = 0.8, n_max: int = 100000, guard: bool = True) -> Optional[int]:
    """Smallest n with power_fn(n) >= target; None beyond n_max.

    Exact tests on counts have saw-toothed power (it can dip below target at a larger n). With ``guard`` the answer
    is the smallest n whose power stays >= target for every n' in [n, n + max(20, n // 10)], found by bisection and
    then by stepping past any dip inside that window."""
    if power_fn(n_max) < target:
        return None
    lo, hi = 1, 2
    while power_fn(hi) < target:
        lo, hi = hi, hi * 2
    while lo < hi:
        mid = (lo + hi) // 2
        if power_fn(mid) >= target:
            hi = mid
        else:
            lo = mid + 1
    if not guard:
        return lo
    n = lo
    while n <= n_max:
        w = max(20, n // 10)
        dips = [k for k in range(n, min(n_max, n + w) + 1) if power_fn(k) < target]
        if not dips:
            return n
        n = dips[-1] + 1
    return None


# ------------------------------------------------------------------------------------------- multiplicity
def holm(pvals: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Holm step-down adjusted p-values (None entries are skipped and stay None)."""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    out: List[Optional[float]] = [None] * len(pvals)
    running = 0.0
    for rank, i in enumerate(sorted(idx, key=lambda i: pvals[i])):
        running = max(running, min(1.0, (m - rank) * pvals[i]))
        out[i] = running
    return out


def bh(pvals: Sequence[Optional[float]]) -> List[Optional[float]]:
    """Benjamini-Hochberg adjusted p-values (q-values); None entries are skipped."""
    idx = [i for i, p in enumerate(pvals) if p is not None]
    m = len(idx)
    out: List[Optional[float]] = [None] * len(pvals)
    running = 1.0
    for rank, i in reversed(list(enumerate(sorted(idx, key=lambda i: pvals[i]), start=1))):
        running = min(running, pvals[i] * m / rank)
        out[i] = min(1.0, running)
    return out


# ------------------------------------------------------------------------------------------- continuous / resampling
def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def sd(xs: Sequence[float]) -> Optional[float]:
    """Sample standard deviation (n - 1); 0 for a single value, None for none."""
    if not xs:
        return None
    if len(xs) == 1:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def percentile(sorted_xs: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of an already sorted list (q in [0, 1])."""
    if not sorted_xs:
        return float("nan")
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_xs) - 1)
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def bootstrap_means(diffs: Sequence[float], clusters: Optional[Sequence[str]] = None, n_boot: int = DEFAULT_BOOT,
                    seed: int = DEFAULT_SEED) -> List[float]:
    """Sorted bootstrap replicates of the mean of ``diffs``.

    With ``clusters`` (one id per diff), whole clusters are resampled with replacement and the replicate is the
    mean over all items drawn (a cluster bootstrap: items that share a template are not treated as independent)."""
    n = len(diffs)
    if n == 0:
        return []
    rng = random.Random(seed)
    reps: List[float] = []
    if clusters is None:
        d = list(diffs)
        for _ in range(n_boot):
            reps.append(sum(rng.choices(d, k=n)) / n)
    else:
        groups: Dict[str, List[float]] = {}
        for x, c in zip(diffs, clusters):
            groups.setdefault(str(c), []).append(x)
        sums = [(sum(v), len(v)) for _, v in sorted(groups.items())]
        g = len(sums)
        for _ in range(n_boot):
            draw = rng.choices(sums, k=g)
            tot = sum(s for s, _ in draw)
            cnt = sum(c for _, c in draw)
            reps.append(tot / cnt)
    reps.sort()
    return reps


def bootstrap_mean_ci(diffs: Sequence[float], conf: float = 0.95, clusters: Optional[Sequence[str]] = None,
                      n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED) -> Tuple[Optional[float], float, float]:
    """(mean, lower, upper): percentile bootstrap interval for the mean paired difference."""
    if not diffs:
        return None, float("nan"), float("nan")
    reps = bootstrap_means(diffs, clusters, n_boot, seed)
    a = (1 - conf) / 2
    return sum(diffs) / len(diffs), percentile(reps, a), percentile(reps, 1 - a)


def tost_bootstrap(reps: Sequence[float], margin: float) -> Dict[str, float]:
    """Equivalence p-value from sorted bootstrap replicates of a mean difference: the larger of the shares of
    replicates at or beyond each margin. 'p < alpha' is exactly 'the rank-based 100(1-2 alpha)% interval
    (``boot_ci_and_p(reps, 2 alpha)``) lies inside +/- margin'."""
    b = len(reps)
    if b == 0:
        return {"p": 1.0, "p_lower": 1.0, "p_upper": 1.0}
    p_lo = sum(1 for r in reps if r <= -margin) / b
    p_hi = sum(1 for r in reps if r >= margin) / b
    return {"p": max(p_lo, p_hi), "p_lower": p_lo, "p_upper": p_hi}


def sign_flip_p(diffs: Sequence[float], n_perm: int = DEFAULT_PERM, seed: int = DEFAULT_SEED,
                exact_max: int = 16) -> float:
    """Two-sided paired permutation p-value for H0: the distribution of each a_i - b_i is symmetric about 0.

    Statistic: |sum of differences|. Exact enumeration when at most ``exact_max`` differences are non-zero,
    otherwise ``n_perm`` random sign flips with the +1 correction."""
    nz = [x for x in diffs if x != 0]
    if not nz:
        return 1.0
    obs = abs(sum(nz)) - 1e-12
    if len(nz) <= exact_max:
        hits = 0
        total = 0
        for signs in itertools.product((1, -1), repeat=len(nz)):
            total += 1
            if abs(sum(s * x for s, x in zip(signs, nz))) >= obs:
                hits += 1
        return hits / total
    rng = random.Random(seed)
    hits = 0
    k = len(nz)
    for _ in range(n_perm):
        bits = rng.getrandbits(k)
        s = 0.0
        for i, x in enumerate(nz):
            s += x if (bits >> i) & 1 else -x
        if abs(s) >= obs:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def seed_spread(values: Sequence[float]) -> Dict[str, Optional[float]]:
    """mean, sample SD, min and max of per-seed values."""
    vs = list(values)
    return {"mean": mean(vs), "sd": sd(vs), "min": min(vs) if vs else None, "max": max(vs) if vs else None,
            "n": len(vs)}


# ------------------------------------------------------------------------------------------- R3: exact TOST (Tango)
def tango_z(b: int, c: int, n: int, delta0: float) -> float:
    """Tango (1998) score statistic for H0: p_A - p_B = delta0 on paired data (b = only A passed, c = only B).

    The restricted MLE of q = P(only B) under H0 solves 2n q^2 + B q + C = 0 with B = -(b + c) + (2n - b + c) delta0,
    C = -c delta0 (1 - delta0); Var(b - c) = n (2q + delta0 (1 - delta0))."""
    if n <= 0:
        return 0.0
    A = 2.0 * n
    B = -(b + c) + (2.0 * n - b + c) * delta0
    C = -c * delta0 * (1.0 - delta0)
    disc = max(0.0, B * B - 4 * A * C)
    q = (-B + math.sqrt(disc)) / (2 * A)
    var = n * (2 * q + delta0 * (1 - delta0))
    num = b - c - n * delta0
    if var <= 0:
        return 0.0 if num == 0 else math.copysign(math.inf, num)
    return num / math.sqrt(var)


def _tango_thresholds(n: int, margin: float, z: float) -> List[int]:
    """t[D] = the largest b (0..D) with tango_z(b, D - b, n, margin) <= z, or -1 (Z rises with b at fixed D)."""
    out = []
    for d in range(n + 1):
        lo, hi = -1, d
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if tango_z(mid, d - mid, n, margin) <= z:
                lo = mid
            else:
                hi = mid - 1
        out.append(lo)
    return out


GRID_SAFETY = 0.99      # The nuisance supremum is taken on a fixed grid; between grid points the size can exceed
                        # the grid supremum by a hair (0.0503 at alpha 0.05, n 1000, psi near 1). Dividing p-values by
                        # 0.99 bounds that: dense off-grid checks (tests, n 50-2548) and an independent full
                        # enumeration (red team r4, n up to 1297) give exact size <= alpha. The grid has no
                        # data-dependent point, so the report's p-rule and power.py's critical rule are one rule.


def _psi_grid(margin: float, extra: Sequence[float] = (), k: int = 80) -> List[float]:
    lo = max(margin, 1e-9)
    g = [lo + (1 - lo) * i / k for i in range(k + 1)]
    return sorted(set(g + [min(1.0, max(lo, e)) for e in extra]))


def _tail_at(n: int, psi: float, delta: float, thr: Sequence[int], upper_lo: Optional[Sequence[int]] = None) -> float:
    """P(b <= thr[D]) (and b >= upper_lo[D] when given) for D ~ Bin(n, psi), b | D ~ Bin(D, (psi + delta)/(2 psi))."""
    ps = min(1.0, max(psi, abs(delta), 1e-12))
    q = min(1.0, max(0.0, (ps + delta) / (2 * ps)))
    mu, sdv = n * ps, math.sqrt(max(n * ps * (1 - ps), 1e-12))
    tot = 0.0
    for d in range(max(0, int(mu - 12 * sdv) - 1), min(n, int(mu + 12 * sdv) + 1) + 1):
        w = math.exp(_binom_logpmf(d, n, ps))
        if w < 1e-15:
            continue
        hi = thr[d]
        lo = 0 if upper_lo is None else upper_lo[d]
        if hi < lo:
            continue
        tot += w * (binom_cdf(hi, d, q) - (binom_cdf(lo - 1, d, q) if lo > 0 else 0.0))
    return min(1.0, tot)


def _exact_one_side(b: int, c: int, n: int, margin: float) -> float:
    """Exact unconditional p for H0: p_A - p_B >= margin (reject for small Tango Z): the supremum over the
    discordance nuisance psi of P(Z <= z_obs) at p_A - p_B = margin."""
    z = tango_z(b, c, n, margin) + 1e-12
    thr = _tango_thresholds(n, margin, z)
    sup = max(_tail_at(n, ps, margin, thr) for ps in _psi_grid(margin))
    return min(1.0, sup / GRID_SAFETY)


def tost_paired_exact(n11: int, n10: int, n01: int, n00: int, margin: float) -> Dict[str, float]:
    """Size-correct TOST for paired proportions within +/- margin: Tango score statistic, exact unconditional
    p-values (supremum over the nuisance on a grid, divided by GRID_SAFETY). p_upper tests H0: d >= +margin,
    p_lower H0: d <= -margin. Unlike the Newcombe-interval TOST (exact size up to 0.065 at alpha 0.05), its exact
    size at the margin is <= alpha for n = 50..1000 (tests/test_stats.py checks a dense off-grid set of psi)."""
    if margin <= 0:
        raise ValueError("margin must be > 0")
    n = n11 + n10 + n01 + n00
    if n == 0:
        return {"p": 1.0, "p_lower": 1.0, "p_upper": 1.0}
    p_hi = _exact_one_side(n10, n01, n, margin)
    p_lo = _exact_one_side(n01, n10, n, margin)        # swap roles: B - A >= margin
    return {"p": max(p_lo, p_hi), "p_lower": p_lo, "p_upper": p_hi}


_ZCRIT: Dict[Tuple[int, float, float], float] = {}


def tost_exact_critical(n: int, margin: float, alpha: float) -> float:
    """The largest Tango z whose exact one-sided p is <= alpha (the rule: reject when Z <= this)."""
    key = (n, round(margin, 9), round(alpha, 12))
    if key in _ZCRIT:
        return _ZCRIT[key]
    grid = _psi_grid(margin)

    def sup_tail(z: float) -> float:
        thr = _tango_thresholds(n, margin, z)
        return max(_tail_at(n, ps, margin, thr) for ps in grid)
    alpha = alpha * GRID_SAFETY
    lo, hi = -40.0, 0.0
    if sup_tail(hi) <= alpha:
        _ZCRIT[key] = hi
        return hi
    for _ in range(50):
        mid = (lo + hi) / 2
        if sup_tail(mid) <= alpha:
            lo = mid
        else:
            hi = mid
    _ZCRIT[key] = lo
    return lo


def tost_exact_size(n: int, psi: float, margin: float, alpha: float) -> float:
    """Exact size of one side of ``tost_paired_exact`` at the boundary d = +margin with discordant share psi
    (any psi, also between grid points). The TOST's size is at most this."""
    z = tost_exact_critical(n, margin, alpha)
    return _tail_at(n, psi, margin, _tango_thresholds(n, margin, z))


def power_tost_paired_exact(n: int, psi: float, margin: float, delta: float = 0.0, alpha: float = 0.05) -> float:
    """Exact power of ``tost_paired_exact`` at alpha when the true difference is delta and the discordant share psi."""
    if n <= 0:
        return 0.0
    z = tost_exact_critical(n, margin, alpha)
    t = _tango_thresholds(n, margin, z)            # b <= t[D]: upper side rejects
    lo = [d - t[d] for d in range(n + 1)]           # swapped: D - b <= t[D]  <=>  b >= D - t[D]
    return _tail_at(n, psi, delta, t, lo)


# ------------------------------------------------------------------------------------------- R3: bootstrap test
def boot_ci_and_p(reps: Sequence[float], alpha: float = 0.05) -> Tuple[float, float, float]:
    """(lo, hi, p) from sorted bootstrap replicates of a mean difference, with decisions that agree exactly:
    p = 2 * min(#(rep <= 0), #(rep >= 0)) / B, and the CI uses ranks ceil(alpha B / 2), so p < alpha <=> 0 outside the CI."""
    B = len(reps)
    if not B:
        return float("nan"), float("nan"), 1.0
    m = max(1, math.ceil(alpha * B / 2))
    lo, hi = reps[m - 1], reps[B - m]
    k_le = sum(1 for r in reps if r <= 0)
    k_ge = sum(1 for r in reps if r >= 0)
    return lo, hi, min(1.0, 2 * min(k_le, k_ge) / B)


def bootstrap_stratified_means(diffs: Sequence[float], strata: Sequence[str], n_boot: int = DEFAULT_BOOT,
                               seed: int = DEFAULT_SEED) -> List[float]:
    """Sorted replicates of the mean over strata of the per-stratum mean difference (each stratum resampled
    within itself): the letter-balanced estimate for MCQ."""
    groups: Dict[str, List[float]] = {}
    for x, s in zip(diffs, strata):
        groups.setdefault(str(s), []).append(x)
    if not groups:
        return []
    rng = random.Random(seed)
    gl = [v for _, v in sorted(groups.items())]
    reps = []
    for _ in range(n_boot):
        reps.append(sum(sum(rng.choices(g, k=len(g))) / len(g) for g in gl) / len(gl))
    reps.sort()
    return reps


def stratified_mean(diffs: Sequence[float], strata: Sequence[str]) -> Optional[float]:
    groups: Dict[str, List[float]] = {}
    for x, s in zip(diffs, strata):
        groups.setdefault(str(s), []).append(x)
    return sum(sum(v) / len(v) for v in groups.values()) / len(groups) if groups else None


# ------------------------------------------------------------------------------------------- R3: kappa power
def kappa_cells(kappa: float, prevalence: float) -> Tuple[float, float, float, float]:
    """(p11, p10, p01, p00) for two raters with equal marginal prevalence and Cohen's kappa."""
    pi = prevalence
    v = pi * (1 - pi)
    p11, p00, off = pi * pi + kappa * v, (1 - pi) ** 2 + kappa * v, v * (1 - kappa)
    if min(p11, p00, off) < -1e-12:
        raise ValueError("kappa not attainable at this prevalence")
    return p11, off, off, p00


def kappa_and_se(a: int, b: int, c: int, d: int) -> Tuple[Optional[float], Optional[float]]:
    """Cohen's kappa of a 2x2 table and its large-sample SE (Fleiss, Cohen & Everitt 1969)."""
    n = a + b + c + d
    if n == 0:
        return None, None
    p = [[a / n, b / n], [c / n, d / n]]
    r = [p[0][0] + p[0][1], p[1][0] + p[1][1]]
    k_ = [p[0][0] + p[1][0], p[0][1] + p[1][1]]
    po = p[0][0] + p[1][1]
    pe = r[0] * k_[0] + r[1] * k_[1]
    if pe >= 1:
        return (1.0 if po == 1 else 0.0), 0.0
    kap = (po - pe) / (1 - pe)
    s1 = sum(p[i][i] * (1 - (r[i] + k_[i]) * (1 - kap)) ** 2 for i in range(2))
    s2 = (1 - kap) ** 2 * sum(p[i][j] * (k_[i] + r[j]) ** 2 for i in range(2) for j in range(2) if i != j)
    s3 = (kap - pe * (1 - kap)) ** 2
    var = max(0.0, (s1 + s2 - s3) / (n * (1 - pe) ** 2))
    return kap, math.sqrt(var)


def power_kappa_lower_ci(kappa: float, n: int, prevalence: float = 0.5, threshold: float = 0.6,
                         conf: float = 0.95, n_sim: int = 2000, seed: int = DEFAULT_SEED) -> float:
    """P(lower 100*conf% Wald CI of Cohen's kappa >= threshold) at n independent units, by simulation. LIBERAL
    (red team r4 S7: exact P(pass | kappa = .6) 0.035-0.124 against 0.025) and blind to units clustered in items:
    kept for reference only; plan with ``power_kappa_cluster`` / ``power_kappa_joint``."""
    p11, p10, p01, _ = kappa_cells(kappa, prevalence)
    rng = random.Random(seed)
    z = z_of(conf)
    hits = 0
    for _ in range(n_sim):
        a, b_, c, d = _multinomial4(rng, n, p11, p10, p01)
        k, se = kappa_and_se(a, b_, c, d)
        if k is not None and se is not None and k - z * se >= threshold:
            hits += 1
    return hits / n_sim


# ------------------------------------------------------------------------------------------- R4: t-TOST, kappa power
def t_cdf(t: float, df: float) -> float:
    """Student t CDF via the regularized incomplete beta."""
    if df <= 0:
        return float("nan")
    if math.isinf(t):
        return 1.0 if t > 0 else 0.0
    x = df / (df + t * t)
    tail = 0.5 * betainc(df / 2.0, 0.5, x)
    return tail if t < 0 else 1.0 - tail


def ttost(diffs: Sequence[float], margin: float) -> Dict[str, Optional[float]]:
    """Paired t-TOST on per-item differences: H0 mean <= -margin and H0 mean >= +margin, p = the larger one-sided
    t p-value. Used for seed groups (per-item mean over seeds) and other continuous equivalence claims; its size
    under a latent-difficulty multi-seed model is checked in tests/test_stats.py."""
    n = len(diffs)
    if n < 2:
        return {"p": 1.0, "p_lower": 1.0, "p_upper": 1.0, "se": None}
    m = sum(diffs) / n
    s_ = sd(diffs) or 0.0
    se = s_ / math.sqrt(n)
    if se == 0:
        lo_ok, hi_ok = m > -margin, m < margin
        return {"p": 0.0 if lo_ok and hi_ok else 1.0, "p_lower": 0.0 if lo_ok else 1.0,
                "p_upper": 0.0 if hi_ok else 1.0, "se": 0.0}
    p_lo = 1.0 - t_cdf((m + margin) / se, n - 1)
    p_hi = t_cdf((m - margin) / se, n - 1)
    return {"p": max(p_lo, p_hi), "p_lower": p_lo, "p_upper": p_hi, "se": se}


def t_ci(diffs: Sequence[float], conf: float) -> Tuple[Optional[float], Optional[float]]:
    """t interval for the mean (the interval that matches ``ttost``: equivalence at alpha <=> the 1-2 alpha t
    interval inside +/- margin)."""
    n = len(diffs)
    if n < 2:
        return None, None
    m = sum(diffs) / n
    se = (sd(diffs) or 0.0) / math.sqrt(n)
    lo, hi = -50.0, 50.0
    target = 0.5 + conf / 2
    for _ in range(100):
        mid = (lo + hi) / 2
        if t_cdf(mid, n - 1) < target:
            lo = mid
        else:
            hi = mid
    q = (lo + hi) / 2
    return m - q * se, m + q * se


def boot_p_mcse(p: Optional[float], n_boot: int) -> Optional[float]:
    """Monte Carlo SE of a bootstrap p-value from n_boot replicates."""
    if p is None or n_boot <= 0:
        return None
    return math.sqrt(max(p * (1 - p), 0.0) / n_boot)


def power_kappa_cluster(kappa: float, n_items: int, units_per_item: int, prevalence: float = 0.5,
                        tau: float = 0.1, threshold: float = 0.6, n_sim: int = 300, n_boot: int = 300,
                        seed: int = DEFAULT_SEED) -> float:
    """P(lower 95% item-cluster bootstrap bound of Cohen's kappa >= threshold): units nested in items, each item
    with its own kappa (kappa + N(0, tau), clipped to what the prevalence allows). Replaces the Wald bound, which is
    liberal (red team r4 S7)."""
    return power_kappa_joint({"all": (n_items, units_per_item, prevalence)}, kappa, tau, threshold, n_sim, n_boot,
                             seed)


def _kappa_from_counts(a: float, b: float, c: float, d: float) -> Optional[float]:
    n = a + b + c + d
    if n <= 0:
        return None
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    return (1.0 if po == 1 else 0.0) if pe >= 1 else (po - pe) / (1 - pe)


def power_kappa_joint(strata: Dict[str, Tuple[int, int, float]], kappa: float, tau: float = 0.1,
                      threshold: float = 0.6, n_sim: int = 300, n_boot: int = 300, seed: int = DEFAULT_SEED,
                      pooled: bool = True) -> float:
    """Joint-pass power of a calibration gate that needs the lower 95% item-cluster bootstrap bound of kappa >=
    threshold in EVERY stratum (and pooled, when ``pooled``). strata: {name: (n_items, units_per_item, prevalence)}."""
    rng = random.Random(seed)
    passes = 0
    for _ in range(n_sim):
        all_items: List[Tuple[int, int, int, int]] = []
        ok = True
        for name, (n_items, upi, prev) in sorted(strata.items()):
            items = []
            for _ in range(n_items):
                k_i = kappa + rng.gauss(0, tau) if tau > 0 else kappa
                lo_k = -min(prev, 1 - prev) / max(prev, 1 - prev)
                k_i = max(lo_k + 1e-9, min(1.0, k_i))
                p11, p10, p01, _ = kappa_cells(k_i, prev)
                items.append(_multinomial4(rng, upi, p11, p10, p01))
            all_items += items
            if ok and _boot_kappa_lower(items, rng, n_boot) < threshold:
                ok = False
        if ok and pooled and len(strata) > 1 and _boot_kappa_lower(all_items, rng, n_boot) < threshold:
            ok = False
        passes += ok
    return passes / n_sim


def _boot_kappa_lower(items: Sequence[Tuple[int, int, int, int]], rng: random.Random, n_boot: int) -> float:
    reps = []
    k = len(items)
    for _ in range(n_boot):
        a = b = c = d = 0
        for it in rng.choices(items, k=k):
            a += it[0]
            b += it[1]
            c += it[2]
            d += it[3]
        v = _kappa_from_counts(a, b, c, d)
        reps.append(-1.0 if v is None else v)
    reps.sort()
    return percentile(reps, 0.025)
