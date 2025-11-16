import os
from datetime import datetime, timedelta
from typing import List, Optional, Literal, Dict, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from database import db, create_document, get_documents
from bson import ObjectId

app = FastAPI(title="Intelligenter Kalender & Planer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utilities ----------

Category = Literal["Arbeit", "Fitness", "Haushalt", "Social", "Lernen", "Persönlich"]
Status = Literal["geplant", "aktiv", "erledigt", "abgesagt"]


def parse_natural_language(text: str) -> Dict[str, Any]:
    """
    Sehr vereinfachte NLP-Logik ohne externe KI-Pakete:
    - Erkennt Dauer ("2 Stunden", "30 Minuten")
    - Erkennt Zeiten ("18 Uhr", "8 bis 15 Uhr")
    - Erkennt Kategorien über Stichworte
    - Erkennt Datumsschlüsselwörter (heute, morgen)
    Diese Funktion ist regelbasiert und kann später durch echte KI ersetzt werden.
    """
    text_low = text.lower()
    result: Dict[str, Any] = {"title": text.strip()}

    # Datum: heute/morgen
    today = datetime.now().date()
    if "morgen" in text_low:
        result["date"] = (today + timedelta(days=1)).isoformat()
    elif "heute" in text_low:
        result["date"] = today.isoformat()

    # Dauer
    minutes = None
    import re
    # z.B. "2 stunden", "1.5 stunden", "30 minuten"
    m = re.search(r"(\d+(?:[\.,]\d+)?)\s*stunden", text_low)
    if m:
        hours = float(m.group(1).replace(',', '.'))
        minutes = int(hours * 60)
    m2 = re.search(r"(\d+)\s*min(?:ute|uten)?", text_low)
    if m2:
        minutes = int(m2.group(1))
    if minutes:
        result["duration_minutes"] = minutes

    # Zeiten
    # "18 uhr" -> start_time 18:00; "8 bis 15 uhr" -> start+end
    m3 = re.search(r"(\d{1,2})\s*uhr", text_low)
    m4 = re.search(r"(\d{1,2})\s*bis\s*(\d{1,2})\s*uhr", text_low)
    if m4:
        start_h, end_h = int(m4.group(1)), int(m4.group(2))
        result["start_time"] = f"{start_h:02d}:00"
        result["end_time"] = f"{end_h:02d}:00"
        if "dauer" not in result and end_h > start_h:
            result["duration_minutes"] = (end_h - start_h) * 60
    elif m3:
        h = int(m3.group(1))
        result["start_time"] = f"{h:02d}:00"

    # Kategorien
    category_map = {
        "ads": "Arbeit",
        "arbeit": "Arbeit",
        "sport": "Fitness",
        "fitness": "Fitness",
        "laufen": "Fitness",
        "einkauf": "Haushalt",
        "einkaufen": "Haushalt",
        "putz": "Haushalt",
        "freunde": "Social",
        "treffen": "Social",
        "lernen": "Lernen",
        "studium": "Lernen",
        "lesen": "Lernen",
        "content": "Arbeit",
        "idee": "Persönlich",
        "notiz": "Persönlich",
    }
    for k, v in category_map.items():
        if k in text_low:
            result["category"] = v
            break

    return result


def iso_for(date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
    if not date_str or not time_str:
        return None
    return datetime.fromisoformat(f"{date_str}T{time_str}:00")


def find_free_slots(day: datetime, existing: List[dict], duration_min: int) -> List[tuple]:
    """
    Findet freie Slots zwischen 08:00 und 20:00 für den gegebenen Tag.
    existing: Liste existierender Blöcke (mit start_iso, end_iso)
    """
    start_day = datetime.combine(day.date(), datetime.min.time()).replace(hour=8)
    end_day = datetime.combine(day.date(), datetime.min.time()).replace(hour=20)

    intervals = []
    for b in existing:
        try:
            s = datetime.fromisoformat(b["start_iso"]) if isinstance(b["start_iso"], str) else b["start_iso"]
            e = datetime.fromisoformat(b["end_iso"]) if isinstance(b["end_iso"], str) else b["end_iso"]
            if s.date() == day.date():
                intervals.append((s, e))
        except Exception:
            continue
    intervals.sort(key=lambda x: x[0])

    # Lücken berechnen
    free = []
    cursor = start_day
    for s, e in intervals:
        if s > cursor:
            if (s - cursor).total_seconds() >= duration_min * 60:
                free.append((cursor, s))
        cursor = max(cursor, e)
    if end_day > cursor and (end_day - cursor).total_seconds() >= duration_min * 60:
        free.append((cursor, end_day))
    return free


# ---------- Models ----------

class NoteInput(BaseModel):
    text: str
    priority: Optional[int] = Field(None, ge=1, le=5)

class Step(BaseModel):
    title: str
    duration_minutes: int
    priority: Optional[int] = None

class PlanPreview(BaseModel):
    steps: List[Step]
    suggested_blocks: List[dict]
    conflicts: List[str] = []

class NaturalInput(BaseModel):
    text: str

class BlockAdjustInput(BaseModel):
    block_id: str
    new_start_iso: Optional[str] = None
    new_end_iso: Optional[str] = None
    extend_minutes: Optional[int] = None


# ---------- Core Logic ----------

def expand_note_to_steps(text: str, priority: Optional[int]) -> List[Step]:
    """Heuristische Schrittgenerierung (2-5 Schritte)."""
    base = parse_natural_language(text)
    title = base.get("title", text)

    # einfache Heuristik: je nach Länge 2-5 Schritte
    words = len(text.split())
    step_count = 2 if words < 6 else 3 if words < 12 else 4 if words < 20 else 5

    total_minutes = base.get("duration_minutes") or 90
    per = max(15, int(total_minutes / step_count))

    steps = []
    for i in range(step_count):
        steps.append(
            Step(
                title=f"{title} – Schritt {i+1}",
                duration_minutes=per,
                priority=priority,
            )
        )
    return steps


def schedule_steps_into_blocks(steps: List[Step], base_info: Dict[str, Any]) -> PlanPreview:
    # Existierende Blöcke aus DB laden
    existing = get_documents("block", {}) if db else []

    day = datetime.now()
    if base_info.get("date"):
        day = datetime.fromisoformat(base_info["date"])

    suggested = []
    conflicts = []

    # Wenn feste Zeit vorhanden ist, dort planen, sonst freie Slots finden
    start_iso = iso_for(base_info.get("date"), base_info.get("start_time"))
    end_iso = iso_for(base_info.get("date"), base_info.get("end_time"))

    cursor = start_iso

    for step in steps:
        dur = step.duration_minutes
        if cursor:  # wir haben eine feste Startzeit
            e = cursor + timedelta(minutes=dur)
            if end_iso and e > end_iso:
                conflicts.append("Schritte passen nicht vollständig in das Zeitfenster.")
            suggested.append(
                {
                    "title": step.title,
                    "category": base_info.get("category"),
                    "start_iso": cursor.isoformat(),
                    "end_iso": e.isoformat(),
                    "duration_minutes": dur,
                    "status": "geplant",
                    "fixed": bool(base_info.get("start_time")),
                }
            )
            cursor = e
        else:
            # freie Slots finden für den Tag
            free = find_free_slots(day, existing + suggested, dur)
            if free:
                s, e = free[0]
                suggested.append(
                    {
                        "title": step.title,
                        "category": base_info.get("category"),
                        "start_iso": s.isoformat(),
                        "end_iso": e if isinstance(e, str) else e.isoformat(),
                        "duration_minutes": dur,
                        "status": "geplant",
                        "fixed": False,
                    }
                )
            else:
                conflicts.append("Kein freies Zeitfenster gefunden. Vorschlag: aufteilen oder anderen Tag wählen.")

    # end_iso strings fix
    for b in suggested:
        if not isinstance(b["end_iso"], str):
            b["end_iso"] = b["end_iso"].isoformat()

    return PlanPreview(steps=steps, suggested_blocks=suggested, conflicts=conflicts)


# ---------- API ----------

@app.get("/")
def root():
    return {"message": "Kalender-Backend aktiv"}


@app.post("/api/notes/preview", response_model=PlanPreview)
def preview_from_note(inp: NoteInput):
    base = parse_natural_language(inp.text)
    steps = expand_note_to_steps(inp.text, inp.priority)
    preview = schedule_steps_into_blocks(steps, base)
    return preview


class ConfirmInput(BaseModel):
    steps: List[Step]
    blocks: List[dict]
    category: Optional[Category] = None
    note_text: Optional[str] = None

@app.post("/api/notes/confirm")
def confirm_plan(inp: ConfirmInput):
    # Save tasks and blocks
    task_ids = []
    for s in inp.steps:
        task_id = create_document(
            "task",
            {
                "title": s.title,
                "duration_minutes": s.duration_minutes,
                "priority": s.priority,
                "category": inp.category,
                "status": "geplant",
            },
        )
        task_ids.append(task_id)

    block_ids = []
    for i, b in enumerate(inp.blocks):
        # link first task for simplicity
        linked_task = task_ids[min(i, len(task_ids) - 1)] if task_ids else None
        block_ids.append(
            create_document(
                "block",
                {
                    **b,
                    "task_id": linked_task,
                },
            )
        )

    return {"tasks": task_ids, "blocks": block_ids}


@app.post("/api/nlp/parse")
def nlp_parse(inp: NaturalInput):
    parsed = parse_natural_language(inp.text)
    # Wenn keine Dauer erkannt: default 60
    parsed.setdefault("duration_minutes", 60)
    return parsed


@app.post("/api/nlp/plan", response_model=PlanPreview)
def nlp_plan(inp: NaturalInput):
    base = parse_natural_language(inp.text)
    steps = [
        Step(title=base.get("title", inp.text), duration_minutes=base.get("duration_minutes", 60))
    ]
    return schedule_steps_into_blocks(steps, base)


@app.get("/api/blocks")
def list_blocks(date: Optional[str] = Query(None, description="YYYY-MM-DD")):
    filt: Dict[str, Any] = {}
    if date:
        start_day = datetime.fromisoformat(f"{date}T00:00:00")
        end_day = start_day + timedelta(days=1)
        docs = get_documents("block", {})
        result = []
        for d in docs:
            s = datetime.fromisoformat(d.get("start_iso"))
            if start_day <= s < end_day:
                d["_id"] = str(d.get("_id"))
                result.append(d)
        return result
    docs = get_documents("block", {})
    for d in docs:
        d["_id"] = str(d.get("_id"))
    return docs


@app.post("/api/blocks/adjust")
def adjust_block(inp: BlockAdjustInput):
    # einfache Neuberechnung: verschiebt/verlängert und passt kollidierende nicht-feste Blöcke nach hinten an
    from pymongo import ReturnDocument

    if not db:
        raise HTTPException(status_code=500, detail="DB nicht verfügbar")

    blocks = db["block"]
    existing = list(blocks.find({}))

    target = next((b for b in existing if str(b.get("_id")) == inp.block_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Block nicht gefunden")

    s = datetime.fromisoformat(target["start_iso"]) if target.get("start_iso") else None
    e = datetime.fromisoformat(target["end_iso"]) if target.get("end_iso") else None

    if inp.new_start_iso:
        s = datetime.fromisoformat(inp.new_start_iso)
    if inp.new_end_iso:
        e = datetime.fromisoformat(inp.new_end_iso)
    if inp.extend_minutes:
        e = e + timedelta(minutes=inp.extend_minutes)

    if not s or not e:
        raise HTTPException(status_code=400, detail="Ungültige Zeiten")

    # Update target
    blocks.update_one({"_id": target["_id"]}, {"$set": {"start_iso": s.isoformat(), "end_iso": e.isoformat()}})

    # Reflow: alle kollidierenden, nicht festen Blöcke am selben Tag werden nach hinten geschoben
    day = s.date()
    others = [b for b in existing if b["_id"] != target["_id"]]
    # sort by start
    def parse_dt(x):
        try:
            return datetime.fromisoformat(x["start_iso"]) if isinstance(x["start_iso"], str) else x["start_iso"]
        except Exception:
            return datetime.min

    others.sort(key=parse_dt)

    for b in others:
        bs = parse_dt(b)
        be = datetime.fromisoformat(b["end_iso"]) if isinstance(b["end_iso"], str) else b["end_iso"]
        if bs.date() != day:
            continue
        # wenn kollidiert und nicht fixed
        if not b.get("fixed") and not (be <= s or bs >= e):
            new_start = e
            dur = int((be - bs).total_seconds() // 60)
            new_end = new_start + timedelta(minutes=dur)
            e = new_end  # Cursor nach hinten
            blocks.update_one({"_id": b["_id"]}, {"$set": {"start_iso": new_start.isoformat(), "end_iso": new_end.isoformat()}})

    return {"status": "ok"}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available" if db is None else "✅ Connected & Working",
    }
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
