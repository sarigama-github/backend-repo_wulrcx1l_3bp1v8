"""
Database Schemas for Intelligent Calendar

Each Pydantic model maps to a MongoDB collection with the lowercase
class name as the collection name.
"""

from pydantic import BaseModel, Field
from typing import Optional, Literal, List

# Categories supported by the planner
Category = Literal[
    "Arbeit",
    "Fitness",
    "Haushalt",
    "Social",
    "Lernen",
    "Persönlich",
]

Status = Literal["geplant", "aktiv", "erledigt", "abgesagt"]

class Task(BaseModel):
    """
    Aufgaben-Schema
    Collection: "task"
    """
    title: str = Field(..., description="Aufgabentitel")
    note: Optional[str] = Field(None, description="Originale Notiz/NLP Eingabe")
    category: Optional[Category] = Field(None, description="Kategorie der Aufgabe")
    duration_minutes: int = Field(..., gt=0, le=24*60, description="Geschätzte Dauer in Minuten")
    priority: Optional[int] = Field(None, ge=1, le=5, description="1 (hoch) bis 5 (niedrig)")
    status: Status = Field("geplant")
    fixed: bool = Field(False, description="Feststehender Block (nicht verschiebbar)")
    date: Optional[str] = Field(None, description="YYYY-MM-DD für geplantes Datum")
    start_time: Optional[str] = Field(None, description="Startzeit HH:MM falls fest")
    end_time: Optional[str] = Field(None, description="Endzeit HH:MM falls fest")

class Block(BaseModel):
    """
    Kalenderblock-Schema
    Collection: "block"
    """
    title: str
    category: Optional[Category] = None
    start_iso: str = Field(..., description="ISO-Startzeit")
    end_iso: str = Field(..., description="ISO-Endzeit")
    duration_minutes: int = Field(..., gt=0)
    status: Status = Field("geplant")
    fixed: bool = Field(False)
    task_id: Optional[str] = Field(None, description="Verknüpfte Task-ID")

class PlanPreview(BaseModel):
    steps: List[Task]
    suggested_blocks: List[Block]
    conflicts: List[str] = []
