"""
Auto-enrichment of goldset with user-evaluated questions.

When a user evaluates a question (gives feedback), we automatically
add it to the goldset_questions_v2 table for future evaluation.
"""
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


def add_question_to_goldset(
    engine: Engine,
    question: str,
    turn_id: str,
    theme: Optional[str] = None,
    goldset_name: str = "auto_enriched",
    source: str = "user",
) -> Optional[int]:
    """
    Add a question to the goldset_questions_v2 table.
    
    Uses ON CONFLICT to avoid duplicates - if question already exists,
    only updates the turn_id if it was missing.
    
    Args:
        engine: SQLAlchemy engine
        question: The question text
        turn_id: The turn_id from chat_runs (8-char format)
        theme: Optional theme (from intent classifier)
        goldset_name: Name of the goldset (default: auto_enriched)
        source: Source type (default: user)
    
    Returns:
        The question ID if inserted/updated, None if failed
    """
    if not engine or not question or not question.strip():
        return None
    
    try:
        with engine.connect() as conn:
            result = conn.execute(text("""
                INSERT INTO goldset_questions_v2 
                    (question, theme, source, goldset_name, original_turn_id)
                VALUES 
                    (:question, :theme, :source, :goldset_name, :turn_id)
                ON CONFLICT (question) DO UPDATE SET
                    theme = COALESCE(goldset_questions_v2.theme, EXCLUDED.theme),
                    original_turn_id = COALESCE(goldset_questions_v2.original_turn_id, EXCLUDED.original_turn_id),
                    updated_at = NOW()
                RETURNING id;
            """), {
                "question": question.strip(),
                "theme": theme,
                "source": source,
                "goldset_name": goldset_name,
                "turn_id": turn_id,
            })
            conn.commit()
            
            row = result.fetchone()
            if row:
                logger.debug(f"Question added to goldset: id={row[0]}, turn_id={turn_id}")
                return row[0]
            return None
            
    except Exception as e:
        logger.warning(f"Failed to add question to goldset: {e}")
        return None


def add_evaluated_question(
    engine: Engine,
    question: str,
    turn_id: str,
    theme: Optional[str] = None,
    response: Optional[str] = None,
    retrieved_context: Optional[list] = None,
    stars: Optional[int] = None,
) -> Optional[int]:
    """
    Add an evaluated question to the goldset and optionally store the response.
    
    This is called when a user submits feedback on a question.
    
    Args:
        engine: SQLAlchemy engine
        question: The question text
        turn_id: The turn_id from chat_runs
        theme: Optional theme from intent classifier
        response: Optional generated response (for goldset_runs)
        retrieved_context: Optional retrieved chunks (for goldset_runs)
        stars: Optional user rating
    
    Returns:
        The question ID if successful, None if failed
    """
    # First, add the question to goldset
    question_id = add_question_to_goldset(
        engine=engine,
        question=question,
        turn_id=turn_id,
        theme=theme,
        goldset_name="beta_evaluated",
        source="user",
    )
    
    # Optionally, we could also store the response in goldset_runs
    # but that's for the next phase (multi-config generation)
    
    return question_id
