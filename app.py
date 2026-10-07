import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import litellm
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Local runs: read KEY=value lines from a .env file next to app.py, so the Google key
# doesn't have to be exported in every new terminal. (.env is git-ignored; Cloud Run uses its env vars.)
# Tolerates "export KEY=value", quotes, spaces and a BOM, since .env files get written many ways.
ENV_FILE = Path(__file__).parent / ".env"
KEY_SOURCE = "environment" if os.environ.get("GOOGLE_PLACES_API_KEY") else None
if ENV_FILE.exists():
    for _line in ENV_FILE.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        _line = _line.strip()
        if _line.startswith("export "):
            _line = _line[len("export "):]
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            _k, _v = _k.strip(), _v.strip().strip('"').strip("'").strip()
            if _k and _v and not os.environ.get(_k):
                os.environ[_k] = _v
                if _k == "GOOGLE_PLACES_API_KEY":
                    KEY_SOURCE = ".env file"

from tools import TOOLS, run_tool  # noqa: E402  (after .env so tools sees the key)

# --- Config ---

SYSTEM_PROMPT = """You are Meowchi, the cat behind "Eat or Not?": a mochi-loving New Yorker foodie cat who helps \
people decide whether a NYC restaurant is worth eating at, using official NYC Health Department records plus \
Google ratings, prices and opening hours. Right now in New York it is {now}.

How to work:
- Never answer from memory about a specific restaurant. Always look it up with your tools.
- Start with search_restaurants to get the restaurant's 'id'. Reuse ids from earlier in the conversation.
- Answer the question the user actually asked. If they ask "has X been shut down?", the first line must answer \
that: whether the Health Department ever closed it (get_inspection_history -> times_closed_by_health_dept and \
closure_dates) and whether Google lists it as closed (worth_it_check -> warning). Don't drift into a general review.
- Choosing a location: if a name matches several places and the user didn't say which, reply with a short \
numbered list (name, address, grade) and ask which one. The page shows them as clickable cards too. Do not \
guess. When the user narrows it down ("manhattan", "the one on Fulton", "#2"), search again with borough / \
zipcode / a street word or pick from the list, and if several still match, ask again. Remember their ORIGINAL \
question and answer it once the place is settled.
- "Is it clean / any violations?" -> get_inspection_history.
- "Rats / mice / pests?" -> rat_risk_report.
- "Is it good / worth it / rating / price?" -> worth_it_check.
- "Is it open (now / tonight / on Sunday)?" -> worth_it_check, which has Google's opening hours and open_now. \
Give the actual hours, e.g. "Open now until 10 PM". Don't confuse "open right now" with "shut down by the city".
- A general "should I eat at X?" or just a restaurant name -> worth_it_check (plus rat_risk_report if pests show up).
- "Where should I eat / best X near Y / which is better?" -> search_restaurants (put ALL the neighborhood's \
zipcodes in one call, e.g. '10012,10013') with cuisine, sort_by best_grade, then ONE compare_restaurants call \
with the top 4-5 ids. Don't call worth_it_check one by one for comparisons.
- If a tool returns an error, read it and fix your call (for example, a shorter name or dropping a filter) before \
giving up.

How to answer (the page renders Markdown, including tables):
- One restaurant: a one-line verdict first (no warm-up sentence before it), then 2-4 short bullets with \
evidence (dates, scores, violations in plain words). Never nest bullets.
- Two or more restaurants: one short intro line, then a Markdown table of compare_restaurants' "ranked" list \
in EXACTLY the returned order (the code already sorted it; never reorder). Columns: \
| # | Restaurant | Verdict | Hygiene | Google rating | Price | Hours today |, e.g. \
| 1 | Milk Bar, 246 Mott St | GO ദ്ദി(• ⩊ •マ | A (7 pts) | ★ 4.4 (650+, adj 4.39) | $ | until 11 PM |. \
Under the table add "_Ranked by verdict, then review-adjusted rating, then fewer violation points._" \
Then one or two sentences on why rank 1 wins, citing its numbers. Don't repeat the table as bullets.
- Only state ratings, prices and hours that a tool returned. If one is missing, write "n/a", never fill it \
from memory or with vague praise like "highly rated".
- If a place is outside the area the user asked about (e.g. Nolita when they said SoHo), say so or leave it out.
- Keep hygiene, rating and price as separate facts; never blend them into one made-up number.
- Rat risk: say which part drives it. Findings "nearby" are city rodent inspections of OTHER buildings within \
150 m, i.e. the block, not the restaurant itself; say so plainly, e.g. "the shop is clean inside, but the block has rats".
- Explain numbers in plain words: an inspection score is violation points, lower is better (A 0-13, B 14-27, \
C 28+); rat risk points are this app's own heuristic (0-2 low, 3-5 moderate, 6+ high).
- Personality: warm, playful cat puns in moderation ("let meow check", "purr-fect", "paws off"), but the facts \
stay exact and you never invent data.
- End the verdict line with exactly one emoticon that matches it:
  recommend (GO, clean and loved): ദ്ദി(• ⩊ •マ
  so-so (FINE, NOT ENOUGH DATA, TASTY BUT CHECK): /ᐠ - ˕ -マ
  avoid (SKIP, closures, HIGH rat risk): /ᐠ ╥ ˕ ╥マ
  can't find it / tool error / can't answer: /ᐠ ·•᷄ ˕ •᷅マ
  asking which location: (˵◝ ⩊ ◜˵マ
- Reply in the same language the user writes in."""

MAX_TOOL_ROUNDS = 6


def nyc_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:  # no tz database in the container: fall back to EDT
        return datetime.now(timezone(timedelta(hours=-4)))


def system_prompt() -> str:
    return SYSTEM_PROMPT.format(now=nyc_now().strftime("%A, %B %d, %Y, %I:%M %p"))

# --- The Harness ---


def run_agent(messages: list[dict]) -> tuple[str, list[dict]]:
    """Complete until the model answers without asking for a tool.

    Returns the final text and a record of every tool call made along the way.
    """
    tool_calls = []

    messages[0]["content"] = system_prompt()  # keep the clock current for long sessions

    for _ in range(MAX_TOOL_ROUNDS):
        reply = litellm.completion(
            model="vertex_ai/gemini-3.5-flash-lite",
            vertex_location="global",
            messages=messages,
            tools=TOOLS,
        ).choices[0].message

        # Append assistant's reply (text, tool calls, or both) to the context.
        # model_dump() keeps it a plain dict: the raw object carries provider-specific
        # fields that trip Pydantic when LiteLLM re-serializes it next round.
        messages += [reply.model_dump()]

        if not reply.tool_calls:
            return reply.content, tool_calls

        # The harness, not the model, runs each tool and appends the result
        for call in reply.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = run_tool(call.function.name, args)
            tool_calls += [{"name": call.function.name, "args": args, "result": result}]

            messages += [{"role": "tool", "tool_call_id": call.id, "content": result}]

    return "Sorry, I hit my tool-call limit before finishing. Try asking about one restaurant at a time.", tool_calls


# --- Session Store ---

# session_id -> list of messages. In-memory, single process.
# Deploy with max instances = 1 so every request reaches the same memory.
sessions: dict[str, list] = {}

# --- FastAPI App ---

app = FastAPI()
# Vendored front-end libraries (Markdown renderer + HTML sanitizer), so the page needs no CDN.
if (Path(__file__).parent / "static").is_dir():  # page falls back to a simple renderer without it
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "index.html")


@app.get("/logo.png")
def logo():
    return FileResponse(Path(__file__).parent / "logo.png")


@app.get("/meowchi.jpg")
def avatar():
    return FileResponse(Path(__file__).parent / "meowchi.jpg")


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest):
    # Get or create the session
    session_id = request.session_id or str(uuid.uuid4())
    if session_id not in sessions:
        sessions[session_id] = [{"role": "system", "content": system_prompt()}]

    # Append user's message to the context
    sessions[session_id] += [{"role": "user", "content": request.message}]

    try:
        response, tool_calls = run_agent(sessions[session_id])
    except Exception as e:
        # Auth, billing, a model that is not running: show it in the chat, not as a 500.
        response, tool_calls = f"Model call failed: {type(e).__name__}: {str(e)[:300]}", []

    return ChatResponse(response=response or "", session_id=session_id, tool_calls=tool_calls)


@app.get("/health")
def health():
    """Diagnostics: is the Google key loaded on this server, and does Google accept it right now?"""
    import requests

    from tools import PLACES_URL

    key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    out = {"google_key_set": bool(key), "google_key_ends_with": key[-4:] if key else None,
           "sessions_in_memory": len(sessions)}
    if key:
        try:
            r = requests.post(PLACES_URL, timeout=10, json={"textQuery": "Katz's Delicatessen New York", "pageSize": 1},
                              headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": "places.displayName,places.rating"})
            out["google_test"] = {"http_status": r.status_code, "response": r.json()}
        except Exception as e:
            out["google_test"] = {"error": f"{type(e).__name__}: {e}"}
    return out


@app.post("/clear")
def clear(session_id: str | None = None):
    sessions.pop(session_id, None)
    return {"status": "ok"}


if __name__ == "__main__":
    # Local runs only (Cloud Run starts uvicorn directly): print the link and open the browser.
    import threading
    import webbrowser

    url = "http://localhost:8000"
    print(f"\n  Eat or Not? is running at {url}  (Ctrl+C to stop)")
    key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if key:
        print(f"  ✅ Google key loaded from the {KEY_SOURCE} (ends with ...{key[-4:]})")
    else:
        print("  ⚠️  GOOGLE_PLACES_API_KEY is NOT set: ratings, prices and hours will be missing.\n"
              f"      Expected a line GOOGLE_PLACES_API_KEY=your-key in {ENV_FILE}"
              + ("" if ENV_FILE.exists() else "  (that file does not exist)"))
    print()
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
