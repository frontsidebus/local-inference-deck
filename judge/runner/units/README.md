# judge runner units (systemd --user)

- `judge-review.path` watches `$JUDGE_REVIEW_DIR/queue/` (`PathChanged=`) and starts
  `judge-review.service`, a oneshot that runs `runner/run_judge.py --pending`.
- Installed by `judge/install.sh --with-units [--start]`, which renders `${JUDGE_DIR}`,
  `${JUDGE_PYTHON}`, `${HERMES_HOME}` and `${JUDGE_REVIEW_DIR}` and copies the units to
  `~/.config/systemd/user/`. Enable only the path unit: `systemctl --user enable --now judge-review.path`.
- The policy lives in the repo's `site.env`. `~/.config/judge/judge.env` (KEY=value lines) is optional and
  **overrides** `site.env` for the units only (it is environment), e.g. `JUDGE_MODE=local` to stop frontier spend.
- `PathChanged` fires on changes only: requests already queued at boot wait for the next change, or
  run `systemctl --user start judge-review.service`. Requests that land while the service runs are picked up
  by the same run (`--pending` re-scans `queue/` before it exits). A request whose judge backend fails stays
  queued and is retried on the next run; after `JUDGE_MAX_ATTEMPTS` (3) it gets a placeholder finding.
- Pause: `systemctl --user stop judge-review.path` (requests keep queueing). Resume: start the path unit, then
  the service once for the backlog.
- Logs: `journalctl --user -u judge-review` and `$JUDGE_REVIEW_DIR/runner.log`.
