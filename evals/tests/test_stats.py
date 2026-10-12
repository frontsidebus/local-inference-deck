"""Unit tests for evals/stats.py against hand-computed or published values."""
import math

import pytest

import stats as ST


def test_wilson_matches_report_values():
    lo, hi = ST.wilson(5, 10)
    assert round(lo, 4) == 0.2366 and round(hi, 4) == 0.7634
    assert ST.wilson(0, 0) == (0.0, 1.0)


def test_mcnemar_exact_hand_values():
    # n = 16 discordant, min = 4: 2 * (C(16,0)+...+C(16,4)) / 2^16 = 2 * 2517 / 65536
    assert ST.mcnemar_exact(12, 4) == pytest.approx(2 * 2517 / 65536)
    # n = 10, min = 2: 2 * (1 + 10 + 45) / 1024
    assert ST.mcnemar_exact(2, 8) == pytest.approx(2 * 56 / 1024)
    assert ST.mcnemar_exact(0, 0) == 1.0 and ST.mcnemar_exact(5, 5) == 1.0
    assert ST.mcnemar_exact(0, 6) == pytest.approx(2 / 64)


def test_holm_hand_values():
    p = [0.01, 0.04, 0.03, 0.005]
    # sorted: .005*4=.02, .01*3=.03, .03*2=.06, .04*1=.04 -> monotone .06
    assert ST.holm(p) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert ST.holm([0.5, None, 0.9]) == [pytest.approx(1.0), None, pytest.approx(1.0)]
    assert ST.holm([0.6, 0.6]) == pytest.approx([1.0, 1.0])  # capped at 1


def test_bh_hand_values():
    p = [0.01, 0.04, 0.03, 0.005]
    # ranks: .005*4/1=.02, .01*4/2=.02, .03*4/3=.04, .04*4/4=.04 (step-up minimum)
    assert ST.bh(p) == pytest.approx([0.02, 0.04, 0.04, 0.02])
    assert ST.bh([None]) == [None]
    # BH never exceeds Holm
    assert all(b <= h + 1e-12 for b, h in zip(ST.bh(p), ST.holm(p)))


def test_newcombe_paired_ci_published_example():
    # Newcombe (1998), Stat Med 17:2635-2650 (paired data), example a=36, b=12, c=2, d=0: method 10 gives 0.0569 to 0.3404
    d, lo, hi = ST.newcombe_paired_ci(36, 12, 2, 0)
    assert d == pytest.approx(0.2)
    assert round(lo, 4) == 0.0569 and round(hi, 4) == 0.3404


def test_newcombe_paired_ci_edge_cases():
    d, lo, hi = ST.newcombe_paired_ci(1, 0, 0, 1)        # perfect agreement: degenerate but defined
    assert d == 0 and lo <= 0 <= hi
    d, lo, hi = ST.newcombe_paired_ci(0, 0, 0, 0)
    assert (d, lo, hi) == (0.0, -1.0, 1.0)
    d, lo, hi = ST.newcombe_paired_ci(0, 10, 0, 0)        # A always wins
    assert d == 1.0 and 0 < lo < 1 and hi == 1.0
    # a 90% interval is inside the 95% one
    _, lo90, hi90 = ST.newcombe_paired_ci(50, 12, 4, 34, 0.90)
    _, lo95, hi95 = ST.newcombe_paired_ci(50, 12, 4, 34, 0.95)
    assert lo95 < lo90 < hi90 < hi95


def test_tost_wald_hand_values():
    # n=100, only A 10, only B 5, margin 0.10: d=.05, psi=.15, se=sqrt((.15-.0025)/100)=0.038406
    r = ST.tost_paired_wald(10, 5, 100, 0.10)
    assert r["se"] == pytest.approx(math.sqrt(0.001475))
    # p_upper = Phi((.05-.10)/.038406) = Phi(-1.3019) ~ 0.0965 (normal table: 0.0968 at 1.30, 0.0951 at 1.31)
    assert r["p_upper"] == pytest.approx(0.0965, abs=5e-4)
    assert r["p_lower"] < 1e-4 and r["p"] == r["p_upper"]


def test_tost_newcombe_is_interval_inclusion():
    n11, n10, n01, n00, m = 140, 8, 6, 46, 0.10
    r = ST.tost_paired_binary(n11, n10, n01, n00, m)
    # at alpha = p, the binding (1 - 2p) bound sits exactly on the margin
    _, lo, hi = ST.newcombe_paired_ci(n11, n10, n01, n00, 1 - 2 * r["p"])
    assert min(abs(lo + m), abs(hi - m)) < 1e-6
    # decision equivalence: p < .05 <=> the 90% interval lies inside +/- margin
    _, lo90, hi90 = ST.newcombe_paired_ci(n11, n10, n01, n00, 0.90)
    assert (r["p"] < 0.05) == (-m < lo90 and hi90 < m)
    # a large difference is never equivalent
    assert ST.tost_paired_binary(10, 30, 0, 60, 0.10)["p"] == 1.0
    with pytest.raises(ValueError):
        ST.tost_paired_binary(1, 1, 1, 1, 0)


def test_mde_mcnemar_inverts_connor_formula():
    # psi = .2, delta = .1: n = (1.95996*sqrt(.2) + 0.84162*sqrt(.19))^2 / .01 = 154.6
    n_needed = (1.959964 * math.sqrt(0.2) + 0.841621 * math.sqrt(0.19)) ** 2 / 0.01
    assert n_needed == pytest.approx(154.6, abs=0.1)
    assert ST.mde_mcnemar(round(n_needed), 0.2) == pytest.approx(0.1, abs=0.002)
    assert ST.mde_mcnemar(0, 0.2) is None
    # more items -> smaller MDE; more discordance -> larger MDE
    assert ST.mde_mcnemar(400, 0.2) < ST.mde_mcnemar(100, 0.2) < ST.mde_mcnemar(100, 0.4)
    # zero observed discordance still gives a finite MDE (psi is floored at delta)
    assert 0 < ST.mde_mcnemar(100, 0.0) < 1


def test_mde_paired_mean():
    assert ST.mde_paired_mean(1.0, 100) == pytest.approx((1.959964 + 0.841621) / 10, abs=1e-5)
    assert ST.mde_paired_mean(1.0, 1) is None


def test_sign_flip_exact_hand_values():
    assert ST.sign_flip_p([1, 1, 1, 1]) == pytest.approx(2 / 16)
    # |sum| >= 2 for 4 signs of +-1: 2 cases of |4| + 8 cases of |2| = 10 of 16
    assert ST.sign_flip_p([1, 1, 1, -1]) == pytest.approx(10 / 16)
    assert ST.sign_flip_p([0, 0]) == 1.0
    # exact sign flip on 0/1 paired differences equals the exact McNemar test
    assert ST.sign_flip_p([1] * 6 + [-1] * 1 + [0] * 20) == pytest.approx(ST.mcnemar_exact(6, 1))


def test_sign_flip_monte_carlo_is_seeded():
    diffs = [0.5] * 25 + [-0.1] * 5
    p1 = ST.sign_flip_p(diffs, n_perm=2000, seed=1)
    assert p1 == ST.sign_flip_p(diffs, n_perm=2000, seed=1)
    assert p1 < 0.01


def test_bootstrap_is_seeded_and_cluster_aware():
    base = [1, 0, 0, 1, 1, 0, 1, 1, 0, 1, -1, 0, 1, 0, 1, 1, 0, 0, 1, -1]
    m, lo, hi = ST.bootstrap_mean_ci(base, n_boot=3000, seed=7)
    assert (m, lo, hi) == ST.bootstrap_mean_ci(base, n_boot=3000, seed=7)
    assert lo < m < hi
    # five template duplicates of every item: the iid bootstrap is too narrow, the cluster bootstrap is not
    dup = [x for x in base for _ in range(5)]
    cl = [str(i) for i in range(len(base)) for _ in range(5)]
    _, ilo, ihi = ST.bootstrap_mean_ci(dup, n_boot=3000, seed=7)
    _, clo, chi = ST.bootstrap_mean_ci(dup, clusters=cl, n_boot=3000, seed=7)
    assert (ihi - ilo) < 0.6 * (chi - clo)
    assert (chi - clo) == pytest.approx(hi - lo, rel=0.15)
    assert ST.bootstrap_mean_ci([0.3] * 10)[1:] == (pytest.approx(0.3), pytest.approx(0.3))


def test_tost_bootstrap_hand_values():
    reps = [-0.3, -0.1, 0.0, 0.05, 0.1, 0.2, 0.25, 0.4, 0.5]
    r = ST.tost_bootstrap(reps, 0.2)
    assert r["p_lower"] == pytest.approx(1 / 9)   # one replicate <= -0.2
    assert r["p_upper"] == pytest.approx(4 / 9)   # four replicates >= 0.2
    assert r["p"] == r["p_upper"]


def test_seed_spread():
    s = ST.seed_spread([0.68, 0.705, 0.73])
    assert s["mean"] == pytest.approx(0.705) and s["sd"] == pytest.approx(0.025) and s["n"] == 3
    assert ST.sd([1.0]) == 0.0 and ST.sd([]) is None


# ------------------------------------------------------------------ round 2: exact and simulated power
def test_binom_cdf_matches_direct_sum():
    for n, p in ((10, 0.3), (37, 0.5), (200, 0.05)):
        for k in (0, 1, n // 3, n - 1):
            direct = sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k + 1))
            assert ST.binom_cdf(k, n, p) == pytest.approx(direct, rel=1e-9, abs=1e-14)
    assert ST.binom_cdf(-1, 5, 0.5) == 0.0 and ST.binom_cdf(5, 5, 0.5) == 1.0


def test_mcnemar_critical_value_agrees_with_the_test():
    for d in (5, 6, 17, 40, 101):
        k = ST.mcnemar_critical(d, 0.05)
        if k >= 0:
            assert ST.mcnemar_exact(k, d - k) < 0.05
        assert ST.mcnemar_exact(k + 1, d - k - 1) >= 0.05
    assert ST.mcnemar_critical(5, 0.05) == -1        # 5 discordant pairs can never reach p < .05 (min p = 1/16)


def test_exact_power_matches_simulation_and_beats_connor():
    for n, psi, pb in ((100, 0.15, 0.6), (200, 0.2, 0.65), (500, 0.1, 0.7)):
        mde = ST.mde_mcnemar_exact(n, psi)
        assert ST.power_mcnemar_exact(n, psi, mde) >= 0.8
        sim = ST.power_mcnemar_sim(n, pb, psi, mde, n_sim=4000, seed=3)
        assert sim == pytest.approx(ST.power_mcnemar_exact(n, psi, mde), abs=0.025)
        # Connor's approximation claims 80% power at a smaller effect than the exact test needs
        assert ST.mde_mcnemar(n, psi) < mde
        assert ST.power_mcnemar_exact(n, psi, ST.mde_mcnemar(n, psi)) < 0.8
    assert ST.power_mcnemar_exact(100, 0.2, 0.0) < 0.05     # size under the null (exact test is conservative)


def test_tost_power_and_mean_power():
    p = ST.power_tost_paired_sim(400, 0.7, 0.1, 0.10, 0.0, 0.05, n_sim=800, seed=1)
    assert p > 0.95
    assert ST.power_tost_paired_sim(30, 0.7, 0.3, 0.10, 0.0, 0.05, n_sim=800, seed=1) < 0.2
    # normal approximations, hand values: ncp = 0.3 * 10 / 1 = 3 -> Phi(3 - 1.96) = 0.8508
    assert ST.power_mean_diff(100, 1.0, 0.3) == pytest.approx(0.8508, abs=1e-3)
    # TOST: se = .1, margin .3, delta 0 -> 2 * Phi(3 - 1.645) - 1 = 0.8248
    assert ST.power_tost_mean(100, 1.0, 0.3) == pytest.approx(0.8248, abs=1e-3)
    assert ST.n_for_power(lambda n: ST.power_mean_diff(n, 1.0, 0.3)) == 88    # (1.96 + .8416)^2 / .09 = 87.2
    with pytest.raises(ValueError):
        ST.paired_cells(0.05, 0.4, 0.0)


def test_power_tool_rows(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("power_tool", str(ST.__file__).replace("stats.py", "tools/power.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    plan = {"alpha": 0.05, "defaults": {"margin": 0.10, "assumed": {"accuracy": {"p_b": 0.65, "psi": 0.2, "delta": 0.1},
                                                                     "mae": {"sd_diff": 1.0, "delta": 0.3}}},
            "planned_n": {"s": 200},
            "families": [{"name": "D1", "test": "iut", "comparisons": [
                {"id": "a", "suite": "s", "a": "x", "b": "y", "direction": "a>b"},
                {"id": "b", "suite": "s", "a": "x", "b": "z", "direction": "a>b"}]},
                {"name": "D5", "comparisons": [
                    {"id": "e", "suite": "s", "a": "x", "b": "y", "direction": "equivalent"},
                    {"id": "m", "suite": "v", "metric": "mae", "a": "x", "b": "y", "direction": "a<b", "n": 100},
                    {"id": "q", "suite": "s", "metric": "mae", "a": "x", "b": "y", "direction": "a<b",
                     "assumed": {"sd_diff": None}}]}]}
    rows = {r["id"]: r for r in P.plan_power(plan, n_sim=400)}
    assert rows["a"]["alpha"] == 0.05 and rows["e"]["alpha"] == pytest.approx(0.05 / 3)   # IUT vs Holm worst case
    assert rows["a"]["power"] == pytest.approx(ST.power_mcnemar_exact(200, 0.2, 0.1, 0.05))
    assert rows["D1 (IUT family)"]["power"] == pytest.approx(rows["a"]["power"] * rows["b"]["power"])
    assert rows["m"]["power"] == pytest.approx(ST.power_mean_diff(100, 1.0, 0.3, 0.05 / 3))
    assert rows["q"]["missing"] == ["sd_diff"]
    assert "**underpowered**" in P.render(list(rows.values())) or all(not r["underpowered"] for r in rows.values())



# ------------------------------------------------------------------ round 3: size-correct TOST, test/CI agreement,
# kappa power, saw-tooth guard
def test_tango_restricted_mle_solves_the_score_equation():
    for b, c, n, d0 in ((10, 4, 100, 0.1), (3, 9, 60, -0.1), (30, 12, 200, 0.05), (0, 5, 50, 0.1)):
        A, B, C = 2.0 * n, -(b + c) + (2.0 * n - b + c) * d0, -c * d0 * (1 - d0)
        q = (-B + math.sqrt(B * B - 4 * A * C)) / (2 * A)
        r = n - b - c
        score = (b / (q + d0) if b else 0) + (c / q if c else 0) - 2 * r / (1 - 2 * q - d0)
        assert abs(score) < 1e-6 * n
        var = n * (2 * q + d0 * (1 - d0))
        assert ST.tango_z(b, c, n, d0) == pytest.approx((b - c - n * d0) / math.sqrt(var))


def test_tango_z_rises_with_b_at_fixed_discordance():
    for n, d in ((100, 30), (200, 17), (60, 60)):
        zs = [ST.tango_z(b, d - b, n, 0.1) for b in range(d + 1)]
        assert all(x < y for x, y in zip(zs, zs[1:]))


@pytest.mark.parametrize("n,margin", [(50, 0.10), (100, 0.10), (200, 0.10), (500, 0.05), (1000, 0.10)])
def test_exact_tost_size_at_the_margin_is_at_most_alpha(n, margin):
    """The red team measured the Newcombe-inversion TOST at 0.057-0.065 at alpha 0.05 (n 60-100). The exact
    Tango TOST's size at d = +margin, over a dense set of nuisance values off the internal grid, stays <= alpha."""
    z = ST.tost_exact_critical(n, margin, 0.05)
    thr = ST._tango_thresholds(n, margin, z)
    pts = [margin + (1 - margin) * (i + 0.37) / 150 for i in range(150)]
    worst = max(ST._tail_at(n, p, margin, thr) for p in pts)
    assert worst <= 0.05, worst


def test_exact_tost_decisions_and_power():
    r = ST.tost_paired_exact(140, 8, 6, 46, 0.10)
    assert r["p"] < 0.001 and r["p"] == max(r["p_lower"], r["p_upper"])
    assert ST.tost_paired_exact(10, 30, 0, 60, 0.10)["p"] > 0.5        # d = 0.3: never equivalent
    assert ST.tost_paired_exact(0, 0, 0, 0, 0.1)["p"] == 1.0
    p0 = ST.power_tost_paired_exact(200, 0.2, 0.1, 0.0, 0.05)
    assert 0.7 < p0 < 0.95 and ST.power_tost_paired_exact(200, 0.2, 0.1, 0.1, 0.05) <= 0.05
    assert ST.power_tost_paired_exact(400, 0.2, 0.1, 0.0, 0.05) > p0


def test_bootstrap_test_agrees_with_its_ci():
    import random
    rng = random.Random(5)
    for k in range(40):
        diffs = [rng.gauss(0.08 * (k % 5 - 2), 1) for _ in range(60)]
        reps = ST.bootstrap_means(diffs, None, 999, seed=k)
        lo, hi, p = ST.boot_ci_and_p(reps, 0.05)
        assert (p < 0.05) == (lo > 0 or hi < 0)
        lo90, hi90, _ = ST.boot_ci_and_p(reps, 0.10)
        tp = ST.tost_bootstrap(reps, 0.3)["p"]
        assert (tp < 0.05) == (-0.3 < lo90 and hi90 < 0.3)


def test_stratified_bootstrap_is_letter_balanced():
    diffs = [1.0] * 9 + [0.0]
    strata = ["A"] * 9 + ["B"]
    assert ST.stratified_mean(diffs, strata) == pytest.approx(0.5)
    reps = ST.bootstrap_stratified_means(diffs, strata, 200, seed=1)
    assert all(r == pytest.approx(0.5) for r in reps)


def test_kappa_se_and_power():
    import random
    p11, p10, p01, p00 = ST.kappa_cells(0.75, 0.4)
    assert (p11 + p00 - (0.4 ** 2 + 0.6 ** 2)) / (1 - (0.4 ** 2 + 0.6 ** 2)) == pytest.approx(0.75)
    rng = random.Random(3)
    ks, ses = [], []
    for _ in range(1500):
        c = ST._multinomial4(rng, 200, p11, p10, p01)
        k, se = ST.kappa_and_se(*c)
        ks.append(k)
        ses.append(se)
    assert ST.sd(ks) == pytest.approx(sum(ses) / len(ses), rel=0.1)       # the SE formula matches the spread
    lo = ST.power_kappa_lower_ci(0.75, 100, 0.5, n_sim=600)
    hi = ST.power_kappa_lower_ci(0.75, 300, 0.5, n_sim=600)
    assert lo < hi and hi > 0.9 and ST.power_kappa_lower_ci(0.6, 300, 0.5, n_sim=600) < 0.1


def test_n_for_power_saw_tooth_guard():
    """Inverted red-team probe: no n at or above the reported n (within the window) has power below target."""
    for psi, delta, alpha in ((0.15, 0.10, 0.05), (0.2, 0.1, 0.01)):
        f = lambda k: ST.power_mcnemar_exact(k, psi, delta, alpha)  # noqa: E731
        n80 = ST.n_for_power(f, 0.8)
        assert all(f(k) >= 0.8 for k in range(n80, n80 + max(20, n80 // 10) + 1))


def test_power_tool_degenerate_and_after_exclusion_n():
    import importlib.util
    spec = importlib.util.spec_from_file_location("power_tool", str(ST.__file__).replace("stats.py", "tools/power.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    r = P.comparison_power("accuracy", "a>b", 200, {"p_b": 0.6, "psi": 0.1, "delta": 0.1}, 0.05)
    assert r["power"] is None and "degenerate" in r
    import json
    from pathlib import Path
    fx = Path(ST.__file__).parent / "tests" / "fixtures" / "rigor_items.jsonl"
    items = [json.loads(l) for l in fx.read_text().splitlines()]
    n_all = P.n_after_exclusions(items, "nvd-cwe", [])
    assert n_all == 14
    assert P.n_after_exclusions(items, "nvd-cwe", ["has_signal"]) == 12
    assert P.n_after_exclusions(items, "nvd-cwe", ["has_signal", "no_dev_neardup"]) == 9
    assert P.n_after_exclusions(items, "nvd-cwe", ["no_name_in_text"]) == n_all - 7


# ------------------------------------------------------------------ round 4
def test_t_cdf_and_ttost_hand_values():
    assert ST.t_cdf(2.0, 10) == pytest.approx(0.96331, abs=1e-5)       # t table
    assert ST.t_cdf(-1.812, 10) == pytest.approx(0.05, abs=2e-4)
    d = [0.1, -0.05, 0.0, 0.05, 0.02, -0.01, 0.03, 0.0]
    r = ST.ttost(d, 0.1)
    m = sum(d) / len(d)
    se = ST.sd(d) / math.sqrt(len(d))
    assert r["p_upper"] == pytest.approx(ST.t_cdf((m - 0.1) / se, 7))
    lo, hi = ST.t_ci(d, 0.90)
    assert (r["p"] < 0.05) == (-0.1 < lo and hi < 0.1)


def test_report_rule_equals_power_rule():
    """r4 S5: no data-dependent grid point, so 'p_upper <= alpha' is exactly 'Z <= the critical z'."""
    n, m, a = 160, 0.1, 0.05
    z = ST.tost_exact_critical(n, m, a)
    for d in (30, 45, 70):
        for b in range(0, d + 1, 3):
            p = ST._exact_one_side(b, d - b, n, m)
            assert (p <= a) == (ST.tango_z(b, d - b, n, m) <= z + 1e-9), (d, b)


@pytest.mark.parametrize("n,margin,alpha,pts", [(1300, 0.05, 0.025, 60), (2548, 0.05, 0.025, 25)])
def test_exact_tost_size_at_c1_sizes(n, margin, alpha, pts):
    z = ST.tost_exact_critical(n, margin, alpha)
    thr = ST._tango_thresholds(n, margin, z)
    grid = [margin + (1 - margin) * (i + 0.41) / pts for i in range(pts)]
    assert max(ST._tail_at(n, p, margin, thr) for p in grid) <= alpha


def test_cluster_kappa_power():
    lo = ST.power_kappa_cluster(0.75, 30, 3, 0.5, 0.1, n_sim=150, n_boot=150)
    hi = ST.power_kappa_cluster(0.75, 120, 3, 0.5, 0.1, n_sim=150, n_boot=150)
    assert lo < hi and hi > 0.8
    size = ST.power_kappa_cluster(0.6, 60, 3, 0.5, 0.0, n_sim=300, n_boot=200)
    assert size < 0.08                                 # near the nominal 2.5% one-sided (MC SE ~0.01)
    joint = ST.power_kappa_joint({"extract": (60, 3, 0.58), "freeform": (60, 3, 0.95)}, 0.75, n_sim=60, n_boot=150)
    single = ST.power_kappa_cluster(0.75, 60, 3, 0.58, n_sim=60, n_boot=150)
    assert joint <= single + 0.1


def test_power_tool_seed_group_and_table(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("power_tool", str(ST.__file__).replace("stats.py", "tools/power.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    r = P.comparison_power("accuracy", "equivalent", 200, {"psi": 0.2, "p_b": 0.6}, 0.05, 0.1, seed_group=True,
                           want_n80=False, seed_pairs=3)
    one = ST.power_tost_paired_exact(200, 0.2, 0.1, 0.0, 0.05)
    assert r["power"] == pytest.approx(one ** 3) and "seed group" in r["note"]
    r2 = P.comparison_power("accuracy", "a>b", 200, {"psi": 0.2, "p_b": 0.6, "delta": 0.08}, 0.05, seed_group=True,
                            want_n80=False)
    assert r2["power"] == pytest.approx(ST.power_mean_diff(200, (0.2 - 0.0064) ** 0.5, 0.08, 0.05))
    import json as _j
    plan = tmp_path / "p.json"
    plan.write_text(_j.dumps({"alpha": 0.05, "planned_n": {"s": 300}, "defaults": {"assumed": {
        "accuracy": {"p_b": 0.6, "psi": 0.2, "delta": 0.1}}}, "families": [{"name": "F", "comparisons": [
            {"id": "x", "suite": "s", "a": "a", "b": "b", "direction": "a>b"}]}]}))
    out = tmp_path / "power_table.md"
    assert P.main(["--comparisons", str(plan), "--out", str(out)]) == 0
    txt = out.read_text()
    assert "generated by evals/tools/power.py" in txt and "| x | s | accuracy | a>b |" in txt
