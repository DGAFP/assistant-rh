"""Regression coverage for display-only math and the real Streamlit call sites."""

import ast
from pathlib import Path
from textwrap import dedent

import pytest
from markdown_it import MarkdownIt
from streamlit.testing.v1 import AppTest

from src.ui.answer_markdown import format_answer_markdown

ROOT = Path(__file__).resolve().parents[1]
FORMULA = r"2,5\ \text{jours} \times 4\ \text{semaines}=10\ \text{jours}"


def test_cet_formula_keeps_its_content_and_list_indentation():
    answer = f"- Calcul :\n  \\[\n  {FORMULA}\n  \\]\n  Ainsi, **10 jours**."
    rendered = format_answer_markdown(answer)
    assert f"  $$\n  {FORMULA}\n  $$" in rendered
    assert rendered.startswith("- Calcul :")
    assert rendered.endswith("  Ainsi, **10 jours**.")
    assert format_answer_markdown(rendered) == rendered


def test_inline_and_compacted_display_math():
    assert format_answer_markdown(r"Minimum : \(4 \times 2,5 = 10\) jours.") == "Minimum : $4 \\times 2,5 = 10$ jours."
    assert format_answer_markdown(r"Calcul : \[2,5 \times 4 = 10\] Suite.") == "Calcul : \n\n$$\n2,5 \\times 4 = 10\n$$\n\n Suite."


@pytest.mark.parametrize(
    "answer",
    [
        "**10 jours**\n\n- Congés\n- RTT\n\n[Guide](https://example.org/guide)",
        r"Une commande `\[x\]` ou ``\(x\)``.",
        "```latex\n\\[x\\]\n```\nFin.",
        "~~~latex\n\\(x\\)\n~~~\nFin.",
        "```latex\n\\[x\\]",  # An unfinished code fence is still code.
        r"Déjà $x + 1$ et \$20.",
        "$$\n\\text{exemple} + 1\n$$",
        r"Littéral : \\[x\\] et \\(x\\).",
        r"Aperçu tronqué : \[2,5 \times 4...",
        r"Délimiteurs vides : \(\) et \[\].",
    ],
)
def test_other_markdown_and_incomplete_formulas_are_unchanged(answer):
    assert format_answer_markdown(answer) == answer


def test_math_after_code_is_still_converted():
    assert format_answer_markdown("```text\n\\[code\\]\n```\nPuis \\(x\\).") == "```text\n\\[code\\]\n```\nPuis $x$."


@pytest.mark.parametrize("answer", [f"**Calcul :**\n\n\\[{FORMULA}\\]\n\n**Suite.**", None])
def test_timeline_uses_complete_answer_and_falls_back_to_trace_preview(answer):
    tree = ast.parse((ROOT / "apps/streamlit-ui/pages/12_Pipeline_Timeline.py").read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_body_generator")
    preview = rf"Calcul : \[{FORMULA}\] Suite..."
    script = (
        "import streamlit as st\n"
        "from src.ui.answer_markdown import format_answer_markdown\n"
        "_metrics_row = lambda values: None\n_fmt_time = str\n"
        f"detail = {{'answer': {answer!r}}}\n" + ast.unparse(function) + f"\n_body_generator({{'answer_preview': {preview!r}}}, {{}}, {{}})"
    )
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert "\\[" not in app.info[0].value
    assert "$$\n" in app.info[0].value
    if answer:
        assert "**Suite.**" in app.info[0].value
        assert "aperçu" not in app.info[0].value
    else:
        assert "aperçu" in app.info[0].value


def test_stream_completion_formats_display_without_rewriting_response():
    source = (ROOT / "apps/streamlit-ui/pages/01_Chatbot.py").read_text()
    start = source.index("                response_placeholder = st.empty()")
    end = source.index("                t_v3_end = time.time()", start)
    answer = rf"Calcul : \[{FORMULA}\]"
    script = (
        "import streamlit as st\n"
        "from src.ui.answer_markdown import format_answer_markdown\n"
        f"stream_generator = iter({[answer[:12], answer[12:]]!r})\n"
        "status_placeholder = st.empty()\n"
        "_stream_clear_on_first = lambda stream, loader: stream\n" + dedent(source[start:end]) + "\nst.session_state['answer'] = v3_response\n"
    )
    app = AppTest.from_string(script).run(timeout=15)
    assert not app.exception
    assert app.session_state["answer"] == answer
    assert app.markdown[0].value == format_answer_markdown(answer).strip()


def test_same_line_display_math_preserves_one_numbered_list():
    answer = r"1. Calcul : \[x\]" + "\n1. Conclusion."
    rendered = format_answer_markdown(answer)
    tokens = MarkdownIt().parse(rendered)
    assert sum(token.type == "ordered_list_open" for token in tokens) == 1
    assert sum(token.type == "list_item_open" for token in tokens) == 2
    assert "   $$\nx" not in rendered
    assert "   $$\n   x\n   $$" in rendered


def test_indented_code_stays_literal_but_list_continuation_is_math():
    code = "Exemple :\n\n    \\[x\\]\n"
    assert format_answer_markdown(code) == code
    answer = "10. Calcul :\n\n    \\[x\\]"
    assert "    $$\n    x\n    $$" in format_answer_markdown(answer)


def test_longer_closing_fence_does_not_hide_following_math():
    answer = "```text\n\\[code\\]\n````\nPuis \\(x\\)."
    assert format_answer_markdown(answer) == "```text\n\\[code\\]\n````\nPuis $x$."


@pytest.mark.parametrize("marker, list_type", [("1.", "ordered_list_open"), ("-", "bullet_list_open")])
def test_formula_only_list_item_stays_in_the_list(marker, list_type):
    answer = marker + r" \[x\]" + "\n" + marker + " Conclusion."
    tokens = MarkdownIt().parse(format_answer_markdown(answer))
    assert sum(token.type == list_type for token in tokens) == 1
    assert sum(token.type == "list_item_open" for token in tokens) == 2
    first_item = next(i for i, token in enumerate(tokens) if token.type == "list_item_open")
    assert tokens[first_item + 1].type != "list_item_close"
