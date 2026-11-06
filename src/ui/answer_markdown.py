"""Presentation-only adaptation of generated math to Streamlit Markdown."""

import re
from itertools import accumulate
from textwrap import dedent, indent

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt()
# Inline constructs never cross a blank line (paragraph break).
_SPAN = r"(?:(?!\n[ \t]*\n).)"
# Inline code and already-supported dollar math must stay literal. Single-dollar
# math follows the Pandoc rule (no inner padding, no digit after the closing
# dollar) so that currency amounts are not mistaken for formulas.
_TOKENS = re.compile(
    rf"(?P<code>(?P<ticks>`+)(?!`){_SPAN}*?(?P=ticks)(?!`))"
    r"|(?P<dollars>(?<![\\$])\$\$.*?(?<!\\)\$\$|(?<![\\$])\$(?![\s$])[^\n$]*?(?<![\s\\])\$(?!\d))"
    r"|(?P<currency>(?<![\\$])\$+)"
    r"|(?P<display>(?<!\\)\\\[(?P<display_body>.*?)\\\])"
    rf"|(?P<inline>(?<!\\)\\\((?P<inline_body>{_SPAN}*?)\\\))",
    re.DOTALL,
)
_QUOTE = re.compile(r"[ \t]*(?:>[ \t]?)*")
_LIST_MARKER = re.compile(r"[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?")


def format_answer_markdown(answer: str) -> str:
    r"""Convert complete \(...\)/\[...\] pairs for display, leaving source data intact.

    Code, dollar math and incomplete expressions (e.g. truncated trace previews)
    stay untouched. Display math gets its own lines, preserving list and quote
    context; inside table rows it stays inline so the row is not split.
    """
    if "\\(" not in answer and "\\[" not in answer:
        return answer
    # The Markdown parser distinguishes indented code from list continuation and
    # handles fenced code, including unfinished and longer closing fences.
    offsets = list(accumulate(map(len, answer.splitlines(keepends=True)), initial=0))
    code_ranges = sorted(
        (offsets[token.map[0]], offsets[token.map[1]]) for token in _MARKDOWN.parse(answer) if token.type in {"fence", "code_block"} and token.map
    )

    pieces: list[str] = []
    currency: list[int] = []
    converted = False
    cursor = 0
    # Code blocks are copied verbatim; only the text between them is scanned.
    for start, end in [*code_ranges, (len(answer), len(answer))]:
        for match in _TOKENS.finditer(answer, cursor, start):
            pieces.append(answer[cursor : match.start()])
            if match.group("currency") is not None:
                currency.append(len(pieces))
            replacement = _convert(answer, match)
            converted |= match.group("currency") is None and replacement != match.group()
            pieces.append(replacement)
            cursor = match.end()
        pieces.append(answer[cursor:end])
        cursor = end

    # New dollar delimiters would pair with a stray currency sign.
    if converted:
        for index in currency:
            pieces[index] = pieces[index].replace("$", r"\$")
    return "".join(pieces)


def _convert(answer: str, match: re.Match[str]) -> str:
    line_start = answer.rfind("\n", 0, match.start()) + 1
    before = answer[line_start : match.start()]
    table_row = before.lstrip().startswith("|")

    if match.group("inline") is not None:
        body = _single_line(match.group("inline_body"), table_row)
        return f"${body}$" if body else match.group()
    if match.group("display") is None:
        return match.group()

    if table_row:
        body = _single_line(match.group("display_body"), table_row)
        return f"${body}$" if body else match.group()

    quote = _QUOTE.match(before).group()
    if ">" not in quote:
        quote = ""
    marker = _LIST_MARKER.match(before, len(quote)).group()
    body = match.group("display_body")
    if quote:
        body = re.sub(r"(?m)^[ \t]*(?:>[ \t]?)+", "", body)
    body = dedent(body).strip()
    if not body:
        return match.group()
    leading = quote + re.sub(r"\S", " ", marker)
    # A quoted blank line keeps the blockquote open.
    blank = quote.rstrip()
    # A blank line after a bare list marker would end the list item.
    separator = "\n" if before == quote + marker and marker.strip() else f"\n{blank}\n"
    body = indent(body, leading, lambda line: bool(quote) or bool(line.strip()))
    return f"{separator}{leading}$$\n{body}\n{leading}$$\n{blank}\n{leading}"


def _single_line(body: str, table_row: bool) -> str:
    body = re.sub(r"[ \t]*\n[ \t]*(?:>[ \t]?)*", " ", body).strip()
    if table_row:
        # A bare pipe would end the table cell.
        body = body.replace(r"\|", r"\Vert{}").replace("|", r"\vert{}")
    return body


def finalize_streamed_answer(placeholder, raw: str) -> str:
    """Re-render a streamed answer for display and return the text kept in history and logs."""
    # GPT-OSS uses <br> for line breaks in tables.
    response = raw.replace("<br>", "<br/>").replace("<br />", "<br/>")
    display = format_answer_markdown(response)
    if "<br" in raw or display != raw:
        placeholder.markdown(display, unsafe_allow_html="<br" in raw)
    return response
