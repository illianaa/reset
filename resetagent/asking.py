"""Runs asking the user: questions only they can answer, and permission for actions a sandboxed run can't take.

A run's question reaches Reset through its engine: Claude Code's AskUserQuestion and permission prompts (its host
channel), Codex's ask_user tool (which Reset serves over MCP) and Codex's approval requests. Reset texts it to the
user with buttons, and the run waits up to runs.questionWaitMinutes for the answer. The answer can come from a
button, a reply to the message, or the user telling Reset's AI in plain words (which Reset confirms in its own
message). Unanswered in time, a question is left to the run and a permission is refused, and that run doesn't ask
again (the user is away). A run asks at most QUESTIONS_PER_RUN times. Waiting spends no tokens, and the run's time
limit pauses meanwhile (never past the next usage reset).

Runs are told never to ask for passwords, tokens or keys, but to have the user set those up on their computer.
"""
from __future__ import annotations

import json
import re
import secrets
import time

from resetagent import channels, config, db, notify
from resetagent.timeutil import local, now

LEFT_TO_YOU = ("The user asked you to decide. Make the most reasonable choice, keep going, and mention it in your "
               "final summary.")
NO_ANSWER = ("Nobody answered in time. Make the most reasonable choice yourself, keep going, and mention it in your "
             "final summary.")
NOT_WAITING = ("The user can't be asked right now. Make the most reasonable choice yourself, keep going, and "
               "mention it in your final summary.")
MAX_WAIT_MINUTES = 120  # the longest runs.questionWaitMinutes can be
RUN_SERVER = "reset_questions"  # the name of the MCP server with ask_user in a Codex run (unlike any of the user's)
QUESTIONS_PER_RUN = 10  # after this many, a run decides by itself and is refused anything it asks permission for
NUMBERS = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:[,\s]+[0-9]+(?:\.[0-9]+)?)*")  # "2", "1.2", "1, 3"


def wait_seconds(cfg: dict) -> float:
    return min(max(0.0, float(cfg["runs"]["questionWaitMinutes"])), MAX_WAIT_MINUTES) * 60


def get(conn, question_id: int):
    return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def waiting(conn, run_id: int | None = None) -> list:
    if run_id is None:
        return conn.execute("SELECT * FROM questions WHERE status = 'waiting' ORDER BY id").fetchall()
    return conn.execute("SELECT * FROM questions WHERE status = 'waiting' AND run_id = ? ORDER BY id",
                        (run_id,)).fetchall()


def _message(row, who: str) -> tuple:
    """The text and buttons that put a question (or a permission request) to the user. The text starts with its
    number, and what the run wrote comes after it, as written (the message isn't reformatted)."""
    body, qid, nonce = json.loads(row["body"]), row["id"], row["nonce"]
    until = local(row["expires_at"])
    if row["kind"] == "permission":
        text = (f"Permission #{qid}: {who} wants to {body['action']}"
                + (f":\n{body['detail']}" if body.get("detail") else ".")
                + (f"\nIts reason: {clip(body['reason'], 300)}" if body.get("reason") else "")
                + f"\nAllow it? If nobody answers by {until}, it's refused. (Or send “allow {qid}” or "
                  f"“deny {qid}”.)")
        return text, [[{"text": "Allow", "data": f"qa:{qid}:{nonce}:y"},
                       {"text": "Deny", "data": f"qa:{qid}:{nonce}:n"}]]
    questions = body["questions"]
    lines, buttons = [f"Question #{qid} from {who[:1].lower() + who[1:]}:"], []  # "from run #7 (Claude)"
    several = len(questions) > 1
    for qi, question in enumerate(questions, 1):
        prefix, number = (f"Q{qi}: ", f"{qi}.") if several else ("", "")
        options = question.get("options") or []
        # Several answers can't be tapped (one tap answers), so they're typed.
        pick = (f" (pick one or more: send “answer {qid} {number}1, {number}{min(2, len(options))}”)"
                if question.get("multiSelect") and options else "")
        lines.append(prefix + clip(question["question"], 600) + pick)
        row_buttons = []
        for oi, option in enumerate(options, 1):
            detail = f": {clip(option['description'], 200)}" if option.get("description") else ""
            lines.append(f"  {oi}. {clip(option['label'], 100)}{detail}")
            row_buttons.append({"text": f"{prefix}{oi}. {option['label']}"[:40], "data": f"qa:{qid}:{nonce}:{qi}.{oi}"})
        if row_buttons and not question.get("multiSelect"):
            buttons.append(row_buttons)
    buttons.append([{"text": "Let it decide", "data": f"qa:{qid}:{nonce}:d"}])
    first = next(((qi, q) for qi, q in enumerate(questions, 1) if q.get("options") and not q.get("multiSelect")),
                 None)
    if first is None:
        lines.append(f"Reply to this message in your own words, or let it decide. If nobody answers by {until}, it "
                     f"decides by itself. (Or send “answer {qid}: …”.)")
    else:
        qi, oi = first[0], min(2, len(first[1]["options"]))
        typed = (f"“answer {qid} {qi}.{oi}” for Q{qi}'s option {oi}" if several else
                 f"“answer {qid} {oi}” for option {oi}")
        lines.append(f"Tap an answer, reply to this message in your own words, or let it decide. If nobody answers by "
                     f"{until}, it decides by itself. (Or send {typed}, or “answer {qid}: …”.)")
    return "\n".join(lines), buttons


def clip(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def ask(conn, cfg: dict, run_id: int, kind: str, body: dict, who: str):
    """Put a question to the user and text it. Returns its row, or None when the run shouldn't wait: asking is off,
    an earlier question from this run went unanswered (the user is away), or it has asked too often."""
    wait = wait_seconds(cfg)
    if wait <= 0 or not channels.reachable(cfg):  # (nobody to ask)
        return None
    before = conn.execute("SELECT COUNT(*) AS asked, COALESCE(SUM(status = 'expired' AND answered_via = 'timeout'), "
                          "0) AS missed FROM questions WHERE run_id = ?", (run_id,)).fetchone()
    if before["missed"]:
        return None
    if before["asked"] >= QUESTIONS_PER_RUN:
        notify.enqueue(conn, "question-closed", f"Run #{run_id} has asked you {QUESTIONS_PER_RUN} times, so it won't "
                       "ask again. It decides by itself, and anything it asks permission for is refused.",
                       dedupe_key=f"questions-capped:{run_id}")
        return None
    at = now()
    qid = conn.execute("INSERT INTO questions(run_id, kind, nonce, body, created_at, expires_at) "
                       "VALUES (?, ?, ?, ?, ?, ?)", (run_id, kind, secrets.token_hex(4), json.dumps(body), at,
                                                     at + wait)).lastrowid
    row = get(conn, qid)
    text, buttons = _message(row, who)
    notify.enqueue(conn, "question", text, dedupe_key=f"question:{qid}", expires_at=row["expires_at"],
                   buttons=buttons)
    return row


def _settle(conn, row, answer: dict, via: str, done: bool = True) -> bool:
    """Save an answer, unless the question changed since it was read (another answer, it closed, its run ended).
    With several questions, answers collect until every question has one (done)."""
    return conn.execute("UPDATE questions SET status = ?, answer = ?, answered_via = ?, answered_at = ? "
                        "WHERE id = ? AND status = 'waiting' AND answer IS ?",
                        ("answered" if done else "waiting", json.dumps(answer), via, now() if done else None,
                         row["id"], row["answer"])).rowcount == 1


def _closed(row) -> str:
    what = f"{'Permission' if row['kind'] == 'permission' else 'Question'} #{row['id']}"
    return {"answered": f"{what} was already answered.",
            "expired": f"{what} closed at {local(row['expires_at'])}.",
            "waiting": f"{what} changed just now. Send your answer again.",
            }.get(row["status"], f"{what} is closed: its run moved on or ended.")


def answer(conn, question_id: int, reply: str, via: str) -> str:
    """Record the user's answer. reply: "2", "1.2" or "1, 3" (options), "decide", "allow"/"deny", or their own
    words. Returns what to tell them."""
    return _answer(conn, question_id, reply, via)[1]


def _answer(conn, question_id: int, reply: str, via: str) -> tuple:
    """(whether the answer was saved, what to tell the user)."""
    row = get(conn, question_id)
    if row is None:
        return False, f"There's no question #{question_id}."
    if row["status"] != "waiting":
        return False, _closed(row)
    text = str(reply).strip()
    run = f"run #{row['run_id']}"
    if row["kind"] == "permission":
        choice = text.lower().rstrip(".!")
        if choice in ("y", "yes", "allow", "ok", "okay", "sure", "go", "go ahead"):
            allow, said = True, f"Allowed: {run} can go ahead."
        elif choice in ("n", "no", "deny", "don't", "dont", "stop", "refuse"):
            allow, said = False, f"Denied: {run} will do without it."
        else:
            return False, f"For permission #{question_id}, reply Allow or Deny."
        return _saved(conn, row, {"allow": allow}, via, said)
    questions = json.loads(row["body"])["questions"]
    answers = (json.loads(row["answer"]) if row["answer"] else {}).get("answers") or {}
    open_questions = [q["question"] for q in questions if q["question"] not in answers]
    if text.lower() in ("d", "decide", "let it decide"):
        answers.update({q: LEFT_TO_YOU for q in open_questions})
        return _saved(conn, row, {"answers": answers, "decided": len(open_questions) == len(questions)}, via,
                      f"OK: {run} will decide by itself.")
    picked = _pick(questions, text) if NUMBERS.fullmatch(text) else None
    if picked is not None:
        answers.update(picked)
        left = [q for q in open_questions if q not in picked]
        said = (f"Got it: {'; '.join(picked.values())}. {len(left)} more to answer on question #{question_id}."
                if left else f"Sent to {run}: " + "; ".join(answers[q["question"]] for q in questions) + ".")
        return _saved(conn, row, {"answers": answers}, via, said, done=not left)
    if NUMBERS.fullmatch(text) and any(q.get("options") for q in questions):  # a mistyped option
        return False, (f"That isn't one of question #{question_id}'s options. Tap one, send “answer {question_id} "
                       f"{'1.2' if len(questions) > 1 else '1'}”, or answer in words.")
    # Their own words answer whatever is still open; the run reads them as it needs.
    answers.update({q: text for q in open_questions})
    return _saved(conn, row, {"answers": answers}, via, f"Sent to {run}: “{text}”.")


def _saved(conn, row, answer: dict, via: str, said: str, done: bool = True) -> tuple:
    """Save the answer and say so, or say why it couldn't be saved."""
    if _settle(conn, row, answer, via, done):
        return True, said
    return False, _closed(get(conn, row["id"]))


def _pick(questions: list, text: str) -> dict | None:
    """Options chosen by number: "2", "1.2" (question 1, option 2), or several like "1, 3" where a question takes
    more than one. None if they aren't all real options."""
    chosen = {}
    for token in re.split(r"[,\s]+", text.strip()):
        qi, _, oi = token.rpartition(".")
        if not qi and len(questions) > 1:
            return None  # which question?
        qi, oi = int(qi or 1), int(oi)
        options = (questions[qi - 1].get("options") or []) if 1 <= qi <= len(questions) else []
        if not 1 <= oi <= len(options):
            return None
        chosen.setdefault(qi - 1, []).append(options[oi - 1]["label"])
    picked = {}
    for qi, labels in chosen.items():
        labels = list(dict.fromkeys(labels))
        if len(labels) > 1 and not questions[qi].get("multiSelect"):
            return None
        picked[questions[qi]["question"]] = ", ".join(labels)
    return picked


def answer_by_ai(conn, question_id: int, text: str) -> dict:
    """Reset's AI passing on what the user said. Reset confirms it in its own message; permission stays the user's."""
    row = get(conn, question_id)
    if row is not None and row["kind"] == "permission":
        return {"sent": False, "reason": "Only the user can allow or deny a run's request: ask them to tap Allow "
                                         "or Deny."}
    saved, reply = _answer(conn, question_id, text, via="ai")
    if not saved:
        return {"sent": False, "reason": reply}  # nothing went to the run, and reply says why
    notify.enqueue(conn, "question-answer", f"Reset's AI answered question #{question_id} for you. {reply}",
                   dedupe_key=f"question-answer:{question_id}:{secrets.token_hex(4)}")
    return {"sent": True, "note": "Reset told the user in its own message what you sent."}


def expire(conn, row):
    """Nobody answered in time: a question is left to the run, a permission is refused. Tells the user. One that
    never reached them (Telegram was down) doesn't count as unanswered: the run may still ask again."""
    sent = conn.execute("SELECT sent_via FROM notifications WHERE dedupe_key = ?",
                        (f"question:{row['id']}",)).fetchone()
    reached = bool(sent and sent["sent_via"] in channels.PEOPLE)
    moved = conn.execute("UPDATE questions SET status = 'expired', answered_via = ?, answered_at = ? "
                         "WHERE id = ? AND status = 'waiting'",
                         ("timeout" if reached else "undelivered", now(), row["id"])).rowcount
    if moved:
        run = f"run #{row['run_id']}"
        latest = get(conn, row["id"])
        if not reached:
            what = "Permission" if row["kind"] == "permission" else "Question"
            text = (f"{what} #{row['id']} from {run} couldn't reach you in time, so "
                    + ("it was refused." if row["kind"] == "permission" else "the run decided by itself."))
        elif row["kind"] == "permission":
            text = f"No answer to permission #{row['id']} in time, so it was refused, and {run} won't ask you again."
        elif latest["answer"]:
            text = (f"Question #{row['id']} wasn't fully answered in time, so {run} took your answers so far, decided "
                    "the rest by itself, and won't ask you again.")
        else:
            text = f"No answer to question #{row['id']} in time, so {run} decided by itself, and won't ask you again."
        notify.enqueue(conn, "question-closed", text, dedupe_key=f"question-closed:{row['id']}")
    return get(conn, row["id"])


def close(conn, question_id: int, via: str) -> None:
    conn.execute("UPDATE questions SET status = 'closed', answered_via = ?, answered_at = ? "
                 "WHERE id = ? AND status = 'waiting'", (via, now(), question_id))


def close_for_run(conn, run_id: int) -> None:
    """A run that ended can't take answers any more."""
    conn.execute("UPDATE questions SET status = 'closed', answered_via = 'run ended', answered_at = ? "
                 "WHERE run_id = ? AND status = 'waiting'", (now(), run_id))


def listing(conn) -> list:
    """Waiting questions, for Reset's AI."""
    items = []
    for row in waiting(conn):
        body = json.loads(row["body"])
        entry = {"question": row["id"], "run": row["run_id"], "kind": row["kind"],
                 "closesAt": local(row["expires_at"])}
        if row["kind"] == "permission":
            entry["wantsTo"] = body["action"] + (f": {body['detail']}" if body.get("detail") else "")
        else:
            entry["asks"] = [{"question": q["question"], "options": [o["label"] for o in q.get("options") or []],
                              **({"pickSeveral": True} if q.get("multiSelect") else {})} for q in body["questions"]]
        items.append(entry)
    return items


# --- Codex runs: Reset's ask_user tool, served over MCP to the run's own Codex -------------------------------

ASK_TOOL = {
    "name": "ask_user",
    "description": "Ask the user something only they can answer: a choice that changes the result, or something "
                   "only they can do, like signing in to a service. Reset texts it to their phone and waits a few "
                   "minutes; if nobody answers, decide yourself. Never ask for passwords, tokens or keys.",
    "inputSchema": {"type": "object", "additionalProperties": False, "required": ["question"], "properties": {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "description": "Choices to tap, if any."}}},
}


class RunTools:
    """The tools a run's own Codex gets from Reset (python -m resetagent ask-server <run>)."""

    def __init__(self, run_id: int):
        self.run_id = run_id

    def definitions(self) -> list:
        return [ASK_TOOL]

    def call(self, name: str, arguments: dict) -> dict:
        if name != "ask_user" or not str(arguments.get("question") or "").strip():
            return {"error": "Use ask_user with a question."}
        conn, cfg = db.connect(), config.load()
        try:
            body = {"questions": [{"question": arguments["question"].strip(), "header": "", "multiSelect": False,
                                   "options": [{"label": str(o), "description": ""}
                                               for o in arguments.get("options") or []][:10]}]}
            row = ask(conn, cfg, self.run_id, "question", body, f"Run #{self.run_id} (Codex)")
            if row is None:
                return {"answer": NOT_WAITING}
            while row["status"] == "waiting":
                if now() >= row["expires_at"]:
                    row = expire(conn, row)
                    break
                time.sleep(1)
                row = get(conn, row["id"])
            return {"answer": reply_text(row)}
        finally:
            conn.close()


def answers_for(row, questions: list) -> dict:
    """Each question's answer for the run, by its text: the user's, or what to do without one (the user may have
    answered only some of several before time ran out)."""
    if row is None:
        return {q["question"]: NOT_WAITING for q in questions}
    given = (json.loads(row["answer"]).get("answers") or {}) if row["answer"] else {}
    missing = LEFT_TO_YOU if row["status"] == "answered" else NO_ANSWER
    return {q["question"]: given.get(q["question"], missing) for q in questions}


def reply_text(row) -> str:
    """What a run is told about its question, in words."""
    if row["status"] != "answered" or not row["answer"]:
        return NO_ANSWER
    answer = json.loads(row["answer"])
    if answer.get("decided"):
        return LEFT_TO_YOU
    answers = answer["answers"]
    if len(answers) == 1:
        return f"The user answered: {next(iter(answers.values()))}"
    return "The user answered: " + "; ".join(f"{q} → {a}" for q, a in answers.items())


def serve_run(run_id: int) -> int:
    from resetagent import mcp  # late import: mcp serves the brain's tools, which read questions from here

    return mcp.serve(toolset=RunTools(run_id))
