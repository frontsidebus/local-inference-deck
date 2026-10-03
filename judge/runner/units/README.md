# judge runner units (systemd --user)

- `judge-review.path` watches `$JUDGE_REVIEW_DIR/queue/` (`PathChanged=`) and starts
  `judge-review.service`, a oneshot that runs `runner/run_judge.py --pending`.
- Installed by `judge/install.sh --with-units [--start]`, which renders `${JUDGE_DIR}`,
  `${JUDGE_PYTHON}`, `${HERMES_HOME}` and `${JUDGE_REVIEW_DIR}` and copies the units to
  `~/.config/systemd/user/`. Enable only the path unit: `systemctl --user enable --now judge-review.path`.
- Optional settings go in `~/.config/judge/judge.env` (KEY=value lines), e.g. `JUDGE_MODE=local`,
  `JUDGE_FRONTIER_DAILY_MAX=10`.
- `PathChanged` fires on changes only: requests already queued at boot wait for the next change, or
  run `systemctl --user start judge-review.service`. A request whose judge backend fails stays queued
  and is retried on the next run; after `JUDGE_MAX_ATTEMPTS` (3) it gets a placeholder finding.
- Logs: `journalctl --user -u judge-review` and `$JUDGE_REVIEW_DIR/runner.log`.
