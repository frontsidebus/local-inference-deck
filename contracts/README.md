# Contracts between the deck's components

The seams between local-inference-deck, agent-judge (`judge/`) and sec-evals (its own repo since plan step 3,
checked out at `$SEC_EVALS_DIR`, else `~/src/deck/sec-evals`) are written down as versioned contract files, so each
side can be tested on its own (plan step 0b). The
contracts are C1-C10 of the repo-split plan. C1 (the site-key registry: `site-keys.yaml`,
`site-keys.schema.json`, `sanitize-policy.yaml`) is described with its own files.

## Rules

- **The producing component owns the contract file and its provider test.** Deck-owned contracts live here.
  A contract owned by agent-judge or sec-evals lives in that component's tree (`judge/contracts/`, sec-evals'
  `evals/contracts/`), so `git filter-repo --path judge/` or `--path evals/` carries it along at the split.
- **Consumers test against a vendored copy**, a byte-identical copy in the consumer's own test fixtures. No
  vendored copy may be in an integrity-pinned set: none is an evals `HARNESS_FILES` entry, so refreshing one never
  forces a re-baseline (`tests/test_vendored.py` checks this against a sec-evals checkout when one is present).
- Every contract carries a **`contract_version`** (`MAJOR.MINOR`):
  - **minor:** additive changes only, such as a new alias, a new optional field, a new unit or a new vector;
  - **major:** a rename or removal, or a change of meaning, such as a changed port, a smaller cap, a changed exit
    code or a changed hash rule.

  Each consumer test asserts the major version it accepts (`ACCEPTS = "1."`). After the split, consumers declare
  the range in `deck.lock` (`requires_contracts`) and `deck check` enforces it.

## Index

| # | Contract | Artifact (owner) | Provider test | Consumer test (vendored copy) |
|---|---|---|---|---|
| C2 | edge ↔ backend: fixed ports, trusted proxy, OIDC ids/groups, auth headers | `edge.yaml` (deck) | `walter/tests/test_port_invariants.py` (ports, reads `edge.yaml`); `tests/test_c2_edge.py` (proxy, OIDC, headers) | none: both sides are in the deck repo until a `spark-edge` split (step 6) |
| C3 | gateway API and aliases | `gateway.yaml` → generated `aliases.json` (deck) | `tests/test_c3_gateway.py`: generator agreement; `litellm.yaml.tmpl`, llama-swap router sets and capabilities, `spark_hooks.py`, codex/opencode/hermes client configs, `site.env.example` `JUDGE_LOCAL_MODEL` | sec-evals `evals/tests/test_contract_aliases.py` (reads `run.py` constants, never edits them); `judge/tests/test_contract_gateway.py` (`JUDGE_LOCAL_MODEL` defaults) |
| C4 | serving identity | `serving-identity/vectors.json`, generated from `serving-identity/llama-swap.contract.yaml`; `serving-identity/serving.schema.json` (deck) | `tests/test_c4_serving_identity.py`: swapcmd reproduces the vectors; the real template carries the `coder` fields (the real-template assertion sec-evals' `test_run.py` made before step 3); `spark-model-agent serving --json` matches the schema; `walter/tests/test_spark_model_serving.py` (swapcmd over the real, rendered and lab templates; no `run.py` import) | sec-evals `evals/tests/test_contract_serving_identity.py`: `run.llama_swap_entry` over `evals/tests/fixtures/llama-swap.contract.yaml`; `gate.live_snapshot` reads the schema's shape. sec-evals `test_run.py` `test_llama_swap_entry_from_template` reads `$SPARK_LLAMA_SWAP_TEMPLATE`, else the fixture (R2) |
| C5 | eval liveness | sec-evals `evals/contracts/eval-liveness.json` | sec-evals `evals/tests/test_contract_liveness.py` (argv, pid layout). Guard liveness: sec-evals `evals/tests/test_gate.py` (`test_guard_that_dies_at_start_refuses_the_run` and the three after it). Site-env resolution: sec-evals `evals/tests/test_site_env.py` (lib.sh); `bin/deck evals` / `install evals`: `tools/tests/test_deck_evals.py` | `tests/test_c5_eval_liveness.py` (`clients/bin/spark-model` `live_runs`, `SPARK_EVAL_REPOS`). The fake-pid test `walter/tests/test_spark_model_client.py::test_live_run_detection` already existed |
| C6 | gate CLI: exit codes 0-4, `gate.json` | sec-evals `evals/contracts/gate-cli.json` | sec-evals `evals/tests/test_contract_gate_cli.py` (constants, docstring, end-to-end records against the schema) | `tests/test_c6_gate_cli.py` (`bin/deck evals gate` execs gate.py; `docs/runbooks/upgrade.md` states the codes) |
| C7 | backend observability: label `llama-swap=1`, `/slots`, unit names | `observability.yaml` (deck) | `tests/test_c7_observability.py` (every rendered model, lab models included: label, name, loopback port, `cmdStop`; deployed units) | `judge/tests/test_contract_observability.py` (probe `slots_summary`, runaway watcher, `KNOWN_UNITS`) |
| C8 | Hermes profile `managed_by` blocks | `hermes-profile.yaml` (deck) | `tests/test_c8_hermes_profile.py` (`clients/install.sh` on a config holding the judge block; both real installers in both orders while `judge/` is here) | `judge/tests/test_contract_hermes_profile.py` (`judge/install.sh` on a config holding the clients block; uninstall leaves it) |
| C9 | frontier env strip list | `judge/contracts/frontier-env.json` (agent-judge) | `judge/tests/test_contract_frontier_env.py` (`run_judge` lists and vectors) | sec-evals `evals/tests/test_contract_frontier_env_parity.py`: parity only, `run.py` keeps its own list |
| C10 | judge ↔ infra repos: `CONVENTIONS.md`, executable `scripts/check-sanitized.sh` | `judge/contracts/infra-repo.json` (agent-judge) | `judge/tests/test_contract_infra_repo.py` (`find_repo` over a multi-repo layout; probe paths). Exit-code handling: already covered by `judge/tests/test_verify.py` (`test_sanitizer_*`); resolution under `JUDGE_REPO_DIR`: `judge/tests/test_probes.py` and `test_boundary_0a.py` | `tests/test_c10_infra_repo.py` (this repo's root, git mode `100755`). Sanitizer exit codes 0/1/2: `scripts/tests/test_check_sanitized.py`. sec-evals has its own consumer test (its root has `CONVENTIONS.md` and the shim since step 3) |

Vendored copies, by consumer (the full lists are `VENDORED`, `SEC_EVALS_IN` and `SEC_EVALS_OUT` in
`tools/contracts.py`):
- sec-evals (in its repo, refreshed from a deck tag): `evals/tests/fixtures/llama-swap.contract.yaml` and
  `evals/tests/fixtures/contracts/` (aliases, serving vectors and schema, frontier-env);
- agent-judge: `judge/tests/fixtures/contracts/` (aliases, observability, hermes-profile);
- deck: `contracts/vendored/sec-evals/` (eval-liveness, gate-cli, and `evals.env.example`, sec-evals' C1 example
  baseline: `tools/tests/test_site_keys.py` checks it equals the registry's `evals` keys and defaults) and
  `contracts/vendored/agent-judge/` (infra-repo).

`tests/test_vendored.py` fails when a copy differs from its source, a generated file is stale, or a contract has
no `contract_version`. The sec-evals copies are compared in both directions only when a sec-evals checkout is
present (`$SEC_EVALS_DIR`, else `~/src/deck/sec-evals`); otherwise the deck's copies only have to exist.

## Changing a contract

1. Edit the source artifact. Bump `contract_version`: the minor number for an additive change, the major number
   otherwise. For `gateway.yaml` or the C4 fixture template, regenerate:

   ```sh
   python3 contracts/tools/contracts.py aliases --write    # aliases.json from gateway.yaml
   python3 contracts/tools/contracts.py vectors --write    # serving-identity/vectors.json from the fixture
   ```

2. Refresh the vendored copies, in the same commit:

   ```sh
   python3 contracts/tools/contracts.py vendor --write     # copies every source over its vendored copies
   python3 contracts/tools/contracts.py vendor             # check only: exit 1 names any copy that differs
   ```

   `vendor --write` copies sec-evals' contracts into `contracts/vendored/sec-evals/` from the checkout, and never
   writes into the checkout: sec-evals copies the deck's artifacts from a deck tag in its own PR.

3. For a major bump, update each consumer test's `ACCEPTS` and the code it covers. Then run:

   ```sh
   python3 -m pytest contracts/tests walter/tests tools/tests -q -p no:cacheprovider
   python3 -m pytest judge/tests/test_contract_*.py -q -p no:cacheprovider
   # in the sec-evals checkout:  python3 -m pytest evals/tests/test_contract_*.py -q -p no:cacheprovider
   ```

The C4 vectors change only when the fixture template or the hash rule changes. A change to the real walter
template does not need a fixture refresh, unless it changes the `coder` fields the contract promises (model id,
a digest-pinned image, `--temp`). Changing those is a major bump.
