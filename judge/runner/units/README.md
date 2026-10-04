# judge runner units (systemd --user)

| Unit | Enabled | What |
|---|---|---|
| `judge-review.path` | yes | `PathChanged=$JUDGE_REVIEW_DIR/queue` starts `judge-review.service`. Only entries of `queue/` itself count: deferred plan requests in `queue/deferred/` do not start the runner (#40). `TriggerLimitIntervalSec=2s`, `TriggerLimitBurst=1000`. `OnFailure=judge-alert@%n.service`. |
| `judge-review.service` | no (triggered) | Oneshot `runner/run_judge.py --pending`: release due deferred requests into `queue/`, judge every ready request, re-scan. `StartLimitIntervalSec=0` in `[Unit]`: no start limit (#40). `OnFailure=judge-alert@%n.service`. |
| `judge-review.timer` | yes | Backstop (#40): starts the service `OnActiveSec=2min` after the timer starts, then `OnUnitInactiveSec=5min` after each run. A failed or stopped path unit delays reviews by minutes, never for good. (`Persistent=` only applies to `OnCalendar=` timers, so it is not set.) |
| `judge-alert@.service` | never (template) | Started by `OnFailure=` with the failed unit as the instance: `runner/alert.py %i` appends `ALERT: judge unit <unit> failed (result=…) … Fix: …` to `$JUDGE_REVIEW_DIR/runner.log` and runs `notify-send` when `DISPLAY`/`WAYLAND_DISPLAY` is set (≤ 1 per unit per `JUDGE_ALERT_MIN_INTERVAL_S`, default 900). It never restarts anything (#41). |

- Installed by `judge/install.sh --with-units [--start]`, which renders `${JUDGE_DIR}`, `${JUDGE_PYTHON}`,
  `${HERMES_HOME}` and `${JUDGE_REVIEW_DIR}` and copies the units to `~/.config/systemd/user/`. It enables the
  path and timer units (plus `watch/judge-runaway-watch.service`), never the service or the template. With
  `--start` it runs `daemon-reload`, `reset-failed`, `enable --now` and `restart` of the path/timer units (so
  changed settings apply to running units); without it, it prints those commands.
- Why the limits (pilot 2, #40): deferred plan requests made most `--pending` runs exit in ~60 ms, so a burst
  of plan writes started the service 5 times in 1 s, hit systemd's default `StartLimitBurst=5` per 10 s, and left
  `judge-review.service` (`start-limit-hit`) and `judge-review.path` (`unit-start-limit-hit`) failed: nothing was
  judged until `reset-failed`. Reproduced and checked with transient `systemd-run --user` units on systemd 255:
  default limits fail after 30 quick writes; `StartLimitIntervalSec=0` + the trigger limits stay active.
- Recover units that are failed anyway:
  `systemctl --user reset-failed judge-review.service judge-review.path && systemctl --user start judge-review.path`,
  then `systemctl --user start judge-review.service` once for the backlog.
- Health: `judge-findings` prints a `WARNING:` (stderr) when a unit is failed or the oldest ready request waited
  more than `JUDGE_STALL_MINUTES` (15); the C5 hook tells the agent once per stall (#41).
- The policy lives in the repo's `site.env`. `~/.config/judge/judge.env` (KEY=value lines) is optional and
  **overrides** `site.env` for the units only (it is environment), e.g. `JUDGE_MODE=local` to stop frontier spend.
- Requests that land while the service runs are picked up by the same run (`--pending` re-scans `queue/` before
  it exits). A request whose judge backend fails stays queued and is retried on the next run (the service exits
  non-zero, which also raises an alert); after `JUDGE_MAX_ATTEMPTS` (3) it gets a placeholder finding.
- Pause: `systemctl --user stop judge-review.path judge-review.timer` (requests keep queueing). Resume: start
  both, then the service once for the backlog.
- Check the files: `systemd-analyze --user verify ~/.config/systemd/user/judge-review.{service,path,timer}`
  prints nothing when they are fine (`judge/tests/test_install.py` runs it on freshly rendered copies).
- Logs: `journalctl --user -u judge-review` and `$JUDGE_REVIEW_DIR/runner.log`.
