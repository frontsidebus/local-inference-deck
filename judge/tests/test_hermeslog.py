"""lib/hermeslog.py: attribution of UNTAGGED parallel tool-call lines to a session (bug #28).

Hermes logs the calls of a concurrent tool batch from worker threads that do not carry the session tag. They
may only be given to a session when its own turn brackets them and no other session was active; another
session's lines must never be attributed. Synthetic interleaved logs of two sessions A and B."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

JUDGE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JUDGE))
sys.path.insert(0, str(JUDGE / "collector"))
from lib import hermeslog as H  # noqa: E402

A = "20261003_224432_aaaaaa"
B = "20261003_224433_bbbbbb"
TZ = timezone.utc


class Log:
    def __init__(self, start="2026-10-03 10:00:00"):
        self.t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
        self.lines = []

    def _add(self, tag, logger, msg, level="INFO", step=1):
        self.t += timedelta(seconds=step)
        ts = self.t.strftime("%Y-%m-%d %H:%M:%S") + ",100"
        self.lines.append(f"{ts} {level} " + (f"[{tag}] " if tag else "") + f"{logger}: {msg}")
        return len(self.lines) - 1

    def start(self, s):
        return self._add(s, "agent.turn_context", f"conversation turn: session={s} model=coder history=0 msg='hi'")

    def api(self, s, n):
        return self._add(s, "agent.conversation_loop", f"API call #{n}: model=coder provider=custom in=10 out=5")

    def end(self, s):
        return self._add(s, "agent.conversation_loop", f"Turn ended: reason=text_response(finish_reason=stop) "
                                                       f"api_calls=2 session={s}")

    def tool(self, name="skill_view", tag=None, how="completed (0.04s, 4549 chars)"):
        return self._add(tag, "agent.tool_executor", f"tool {name} {how}")

    def noise(self):
        return self._add(None, "hermes_cli.mem_trim", "memory trim: reason=housekeeping")

    def write(self, path):
        path.write_text("\n".join(self.lines) + "\n")
        return path


def window():
    return datetime(2026, 10, 3, 9, 0, tzinfo=TZ), datetime(2026, 10, 3, 12, 0, tzinfo=TZ)


def test_only_one_session_active_parallel_lines_are_attributed():
    lg = Log()
    lg.start(A)
    lg.api(A, 1)
    t1, t2 = lg.tool("skill_view"), lg.tool("read_file")
    lg.noise()
    lg.api(A, 2)
    t3 = lg.tool("search_files", how="failed (0.02s): {\"error\": \"Path not found\"}")
    lg._add(A, "agent.tool_executor", "Tool search_files returned error (0.02s): x", level="WARNING")
    lg.api(A, 3)
    lg.end(A)
    assert H.parallel_attribution(lg.lines) == {t1: A, t2: A, t3: A}


def test_interleaved_sessions_never_get_each_others_lines(tmp_path):
    lg = Log()
    # phase 1: only A is active -> A's parallel lines are A's
    lg.start(A)
    lg.api(A, 1)
    a1, a2 = lg.tool("skill_view"), lg.tool("read_file")
    lg.api(A, 2)
    lg.end(A)
    # phase 2: A and B run at the same time, lines interleaved -> nobody gets the untagged lines
    lg.start(A)
    lg.start(B)
    lg.api(A, 1)
    x1 = lg.tool("search_files")
    lg.api(B, 1)
    x2 = lg.tool("read_file")
    lg.api(A, 2)
    lg.api(B, 2)
    lg.end(A)
    lg.end(B)
    # phase 3: B's turn is open (waiting on a long API call, no B line) while A's bracket closes
    lg.start(B)
    lg.api(B, 1)
    lg.start(A)
    lg.api(A, 1)
    x3 = lg.tool("skill_view")
    lg.api(A, 2)
    lg.end(A)
    lg.end(B)
    # phase 4: only B is active -> B's lines are B's, never A's
    lg.start(B)
    lg.api(B, 1)
    b1 = lg.tool("terminal")
    lg.api(B, 2)
    lg.end(B)
    # an untagged tool line outside any bracket (after a turn start, before the first API call)
    lg.start(A)
    x4 = lg.tool("memory")
    lg.api(A, 1)
    lg.end(A)
    att = H.parallel_attribution(lg.lines)
    assert att == {a1: A, a2: A, b1: B}
    assert not {x1, x2, x3, x4} & set(att)

    log = lg.write(tmp_path / "agent.log")
    since, until = window()
    tagged, context, dropped = H.split_lines(log, A, since, until, TZ)
    marked = [ln for ln in tagged if ln.endswith(H.PARALLEL_MARK)]
    assert [ln[:-len(H.PARALLEL_MARK)] for ln in marked] == [lg.lines[a1], lg.lines[a2]]
    assert lg.lines[b1] not in "\n".join(tagged)
    assert all(lg.lines[i] in context for i in (x1, x2, x3, x4, b1))
    assert all(f"[{B}]" not in ln for ln in tagged)
    tagged_b, context_b, _ = H.split_lines(log, B, since, until, TZ)
    assert [ln for ln in tagged_b if ln.endswith(H.PARALLEL_MARK)] == [lg.lines[b1] + H.PARALLEL_MARK]
    assert lg.lines[a1] in context_b

    # the enqueue hook's counters and the collector's tool attribution see them too
    assert H.tool_activity(log, A, since, until, TZ) == 2
    assert H.tools_used(log, A, since, until, TZ) == {"skill_view", "read_file"}
    assert H.tools_used(log, B, since, until, TZ) == {"terminal"}
    lines_a = H.session_lines(log, A, since, until, TZ, include_untagged=False)
    assert lg.lines[a1] + H.PARALLEL_MARK in lines_a and lg.lines[x1] not in lines_a
    assert lg.lines[a1] not in H.session_lines(log, A, since, until, TZ, include_untagged=False,
                                               attribute_parallel=False)


def test_unterminated_other_turn_blocks_for_a_while_only():
    lg = Log()
    lg.start(B)
    lg.api(B, 1)  # B never logs `Turn ended` (crashed)
    lg.start(A)
    lg.api(A, 1)
    x = lg.tool()
    lg.api(A, 2)
    lg.end(A)
    lg.t += H.OPEN_TURN_IDLE + timedelta(minutes=1)
    lg.start(A)
    lg.api(A, 1)
    a = lg.tool()
    lg.api(A, 2)
    lg.end(A)
    assert H.parallel_attribution(lg.lines) == {a: A}


def test_session_first_seen_mid_turn_counts_as_busy():
    """The scanned tail can start in the middle of B's turn: B is busy from the start of the tail."""
    lg = Log()
    lg.api(B, 7)
    lg.start(A)
    lg.api(A, 1)
    x = lg.tool()
    lg.api(A, 2)
    lg.end(A)
    lg.end(B)
    assert H.parallel_attribution(lg.lines) == {}


def test_collector_puts_attributed_lines_in_session_section_and_claims_count_them(tmp_path, monkeypatch):
    from collector_testlib import make_env
    import collect
    import claims_only as CO
    from lib import config
    e = make_env(tmp_path, monkeypatch)
    monkeypatch.setenv("JUDGE_LOG_TZ", "UTC")
    lg = Log()
    lg.start(A)
    lg.api(A, 1)
    lg.tool("skill_view")
    lg.tool("read_file")
    lg.api(A, 2)
    lg.end(A)
    lg.write(e["hermes"] / "logs" / "agent.log")
    (e["hermes"] / "logs" / "errors.log").write_text("")
    since, until = window()
    text = collect.hermes_log({"session": A, "kind": "completion"}, config.load_config(), since, until)
    sess = text.split("===== agent.log: SESSION LINES")[1].split("\n=====")[0]
    assert sess.count(H.PARALLEL_MARK) == 2
    ev = tmp_path / "ev"
    ev.mkdir()
    (ev / "hermes-log.txt").write_text(text)
    summary, events, _ = CO.tool_activity(ev, A)
    assert summary["tool_calls"] == 2 and summary["parallel_tool_calls"] == 2
    assert summary["untagged_tool_lines"] == 0
