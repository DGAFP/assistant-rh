"""Presentation-only adaptation of generated math to Streamlit Markdown."""

import re
from itertools import accumulate
from textwrap import dedent, indent

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt()
# Inline code and already-supported dollar math must stay literal.
_TOKENS = re.compile(
    r"(?P<code>(?P<ticks>`+)(?!`).*?(?P=ticks)(?!`))"
    r"|(?P<dollars>(?<![\\$])\$\$.*?(?<!\\)\$\$|(?<![\\$])\$(?!\$)[^\n]*?(?<!\\)\$)"
    r"|(?P<display>(?<!\\)\\\[(?P<display_body>.*?)\\\])"
    r"|(?P<inline>(?<!\\)\\\((?P<inline_body>[^\n]*?)\\\))",
    re.DOTALL,
)


def format_answer_markdown(answer: str) -> str:
    r"""Convert complete \(...\)/\[...\] pairs for display, leaving source data intact.

    Code, dollar math and incomplete expressions (e.g. truncated trace previews)
    stay untouched. Display math gets its own lines, preserving list indentation.
    """
    # The Markdown parser distinguishes indented code from list continuation and
    # handles fenced code, including unfinished and longer closing fences.
    offsets = list(accumulate(map(len, answer.splitlines(keepends=True)), initial=0))
    code_ranges = [
        (offsets[token.map[0]], offsets[token.map[1]]) for token in _MARKDOWN.parse(answer) if token.type in {"fence", "code_block"} and token.map
    ]

    def replace(match: re.Match[str]) -> str:
        if any(start < match.end() and match.start() < end for start, end in code_ranges):
            return match.group()
        if match.group("inline") is not None:
            body = match.group("inline_body").strip()
            return f"${body}$" if body else match.group()
        if match.group("display") is not None:
            body = dedent(match.group("display_body")).strip()
            if not body:
                return match.group()
            line_start = answer.rfind("\n", 0, match.start()) + 1
            before = answer[line_start : match.start()]
            prefix = re.match(r"[ \t]*(?:(?:[-+*]|\d+[.)])[ \t]+)?", before).group()
            leading = re.sub(r"\S", " ", prefix)
            # A blank line after a bare list marker would end the list item.
            separator = "\n" if before == prefix and prefix.strip() else "\n\n"
            return f"{separator}{leading}$$\n{indent(body, leading)}\n{leading}$$\n\n{leading}"
        return match.group()

    return _TOKENS.sub(replace, answer)
