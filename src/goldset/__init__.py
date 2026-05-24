"""
Goldset management module.
Auto-enrichment of goldset with user-evaluated questions.
"""
from .auto_enrich import add_evaluated_question, add_question_to_goldset

__all__ = ["add_question_to_goldset", "add_evaluated_question"]
