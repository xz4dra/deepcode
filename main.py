"""DeepCode - a small, keyboard-first TUI code editor built with Textual."""

from __future__ import annotations

import argparse
import inspect
import os
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widgets import Button, Input, Static, TextArea
from textual.widgets.text_area import LanguageDoesNotExist, TextAreaTheme

from deepcode_api import APIError, LocalAPIServer, WorkspaceAPI

# ---------------------------------------------------------------------------
# Logo
# ---------------------------------------------------------------------------

# A 5x7 pixel font.  "█" is the solid body and "▓" is the dithered inner
# fill, which gives the wordmark the chunky opencode-style pixel look.
_GLYPHS: dict[str, list[str]] = {
    "D": [
        "█████",
        "█▓▓▓█",
        "█▓▓▓█",
        "█▓▓▓█",
        "█▓▓▓█",
        "█████",
        "     ",
    ],
    "e": [
        "     ",
        "█████",
        "█▓▓▓▓",
        "█████",
        "█▓▓▓▓",
        "█████",
        "     ",
    ],
    "p": [
        "     ",
        "█████",
        "█▓▓▓█",
        "█▓▓▓█",
        "█████",
        "█    ",
        "█    ",
    ],
    "C": [
        "█████",
        "█▓▓▓▓",
        "█▓▓▓▓",
        "█▓▓▓▓",
        "█▓▓▓▓",
        "█████",
        "     ",
    ],
    "o": [
        "     ",
        "█████",
        "█▓▓▓█",
        "█▓▓▓█",
        "█▓▓▓█",
        "█████",
        "     ",
    ],
    "d": [
        "   ██",
        "   ██",
        "█████",
        "█▓▓▓█",
        "█▓▓▓█",
        "█████",
        "     ",
    ],
}


def _build_logo(word: str) -> str:
    rows: list[str] = []
    for row in range(7):
        rows.append("  ".join(_GLYPHS[letter][row] for letter in word))
    return "\n".join(rows)


LOGO = _build_logo("DeepCode")


# ---------------------------------------------------------------------------
# Persistence model
# ---------------------------------------------------------------------------


@dataclass
class Document:
    """One open file (or one unsaved buffer)."""

    name: str
    path: Path | None = None
    text: str = ""
    saved_text: str = ""
    cursor: tuple[int, int] = (0, 0)
    scroll_y: int = 0
    modified: bool = False


# ---------------------------------------------------------------------------
# Syntax highlighting
# ---------------------------------------------------------------------------

# Textual 8 ships tree-sitter grammars for these language identifiers.
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".pyi": "python",
    ".json": "json",
    ".jsonc": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".markdown": "markdown",
    ".mdx": "markdown",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".vue": "html",
    ".svelte": "html",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".css": "css",
    ".tcss": "css",
    ".scss": "css",
    ".sass": "css",
    ".less": "css",
    ".xml": "xml",
    ".svg": "xml",
    ".java": "java",
    ".sql": "sql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "bash",
    ".rs": "rust",
    ".go": "go",
}

_SYNTAX_COLOR_DEFAULTS: dict[str, str] = {
    "keyword": "#FF7B72",
    "string": "#A5D6FF",
    "comment": "#8B949E",
    "function": "#D2A8FF",
    "type": "#FFA657",
    "number": "#79C0FF",
    "constant": "#79C0FF",
    "variable": "#C9D1D9",
    "property": "#7EE787",
    "operator": "#C9D1D9",
    "punctuation": "#C9D1D9",
    "tag": "#7EE787",
    "heading": "#D2A8FF",
    "error": "#F85149",
}


def detect_language(path: Path | None, text: str = "") -> str | None:
    """Return a Textual/tree-sitter language name for a file."""

    if path is not None:
        language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language is not None:
            return language

    first_line = text.splitlines()[0] if text else ""
    if first_line.startswith("#!"):
        lowered = first_line.lower()
        if "python" in lowered:
            return "python"
        if "bash" in lowered or lowered.endswith("/sh"):
            return "bash"

    return None


def _load_syntax_colors() -> dict[str, str]:
    """Read the syntax palette from style.tcss.

    Textual exposes syntax token colours through ``TextAreaTheme`` rather than
    through TCSS properties, so the palette lives in style.tcss as ``$dc-*``
    variables and is converted into the editor theme here.
    """

    colors = dict(_SYNTAX_COLOR_DEFAULTS)
    css_path = Path(__file__).with_name("style.tcss")
    try:
        source = css_path.read_text(encoding="utf-8")
    except OSError:
        return colors

    pattern = re.compile(
        r"^\s*\$dc-([a-z-]+)\s*:\s*(#[0-9A-Fa-f]{6})\s*;",
        re.MULTILINE,
    )
    for name, value in pattern.findall(source):
        if name in colors:
            colors[name] = value
    return colors


def build_deepcode_theme() -> TextAreaTheme:
    colors = _load_syntax_colors()

    def color(name: str) -> str:
        return colors[name]

    syntax_styles: dict[str, Style] = {
        # Keywords / control flow.
        "keyword": Style(color=color("keyword")),
        "keyword.function": Style(color=color("keyword")),
        "keyword.return": Style(color=color("keyword")),
        "keyword.operator": Style(color=color("keyword")),
        "conditional": Style(color=color("keyword")),
        "repeat": Style(color=color("keyword")),
        "include": Style(color=color("keyword")),
        "exception": Style(color=color("keyword")),
        "preproc": Style(color=color("keyword")),
        "import": Style(color=color("keyword")),
        "media": Style(color=color("keyword")),
        "supports": Style(color=color("keyword")),
        "charset": Style(color=color("keyword")),
        "namespace": Style(color=color("keyword")),
        "keyframes": Style(color=color("keyword")),
        # Strings.
        "string": Style(color=color("string")),
        "string.escape": Style(color=color("string")),
        "string.documentation": Style(color=color("string"), italic=True),
        "text.literal": Style(color=color("string")),
        "inline_code": Style(color=color("string")),
        "link.uri": Style(color=color("string"), underline=True),
        # Comments.
        "comment": Style(color=color("comment"), italic=True),
        "conceal": Style(color=color("comment")),
        # Functions / methods.
        "function": Style(color=color("function")),
        "function.call": Style(color=color("function")),
        "function.builtin": Style(color=color("function")),
        "method": Style(color=color("function")),
        "method.call": Style(color=color("function")),
        "constructor": Style(color=color("function")),
        # Types / classes.
        "type": Style(color=color("type")),
        "type.class": Style(color=color("type")),
        "type.builtin": Style(color=color("type")),
        "type.definition": Style(color=color("type")),
        "type.qualifier": Style(color=color("type")),
        "class": Style(color=color("type")),
        # Numbers / constants.
        "number": Style(color=color("number")),
        "float": Style(color=color("number")),
        "boolean": Style(color=color("constant"), italic=True),
        "constant": Style(color=color("constant")),
        "constant.builtin": Style(color=color("constant")),
        "json.null": Style(color=color("constant"), italic=True),
        "none": Style(color=color("constant"), italic=True),
        # Variables / fields / properties.
        "variable": Style(color=color("variable")),
        "variable.builtin": Style(color=color("variable")),
        "parameter": Style(color=color("variable")),
        "field": Style(color=color("property")),
        "property": Style(color=color("property")),
        "json.label": Style(color=color("property")),
        "yaml.field": Style(color=color("property"), bold=True),
        "toml.type": Style(color=color("property")),
        "css.property": Style(color=color("property")),
        "attribute": Style(color=color("property")),
        "attribute.builtin": Style(color=color("property")),
        # Punctuation / operators.
        "operator": Style(color=color("operator")),
        "punctuation.bracket": Style(color=color("punctuation")),
        "punctuation.delimiter": Style(color=color("punctuation")),
        "punctuation.special": Style(color=color("punctuation")),
        # Tags / markdown.
        "tag": Style(color=color("tag")),
        "heading": Style(color=color("heading"), bold=True),
        "heading.marker": Style(color=color("comment")),
        "list.marker": Style(color=color("comment")),
        "link.label": Style(color=color("function")),
        # Errors and inline markup.
        "error": Style(color=color("error"), underline=True),
        "json.error": Style(color=color("error"), underline=True),
        "bold": Style(bold=True),
        "italic": Style(italic=True),
        "strikethrough": Style(strike=True),
    }

    return TextAreaTheme(
        name="deepcode",
        base_style=Style(color="#C9D1D9", bgcolor="#0D1117"),
        gutter_style=Style(color="#484F58", bgcolor="#0D1117"),
        cursor_style=Style(color="#0D1117", bgcolor="#C9D1D9"),
        cursor_line_style=Style(bgcolor="#161B22"),
        cursor_line_gutter_style=Style(
            color="#C9D1D9", bgcolor="#21262D", bold=True
        ),
        selection_style=Style(bgcolor="#264F78", color="#FFFFFF"),
        bracket_matching_style=Style(bgcolor="#3B4252"),
        syntax_styles=syntax_styles,
    )


# ---------------------------------------------------------------------------
# Editor widget
# ---------------------------------------------------------------------------


class CodeEditor(TextArea):
    """TextArea with line numbers, syntax highlighting and search matches."""

    def __init__(self, **kwargs) -> None:
        super().__init__(
            soft_wrap=False,
            show_line_numbers=True,
            tab_behavior="indent",
            **kwargs,
        )
        self.register_theme(build_deepcode_theme())
        self.theme = "deepcode"
        self._search_ranges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self._search_current = -1
        self._match_style = Style(bgcolor="#3A3018", color="#E6EDF3")
        self._current_match_style = Style(bgcolor="#9E6A03", color="#FFFFFF")

    # -- search ---------------------------------------------------------

    def set_search_ranges(
        self,
        ranges: list[tuple[tuple[int, int], tuple[int, int]]],
        current: int = -1,
    ) -> None:
        self._search_ranges = ranges
        self._search_current = current
        self._line_cache.clear()
        self.refresh()

    def set_search_current(self, current: int) -> None:
        self._search_current = current
        self._line_cache.clear()
        self.refresh()

    def clear_search(self) -> None:
        self.set_search_ranges([], -1)

    def _render_line(self, y: int) -> Strip:
        strip = super()._render_line(y)
        if not self._search_ranges or self.soft_wrap:
            return strip

        scroll_x, scroll_y = self.scroll_offset
        line_index = int(scroll_y) + y
        gutter = self.gutter_width

        for index, (start, end) in enumerate(self._search_ranges):
            if start[0] != line_index:
                continue
            start_column = self.get_column_width(line_index, start[1])
            end_column = self.get_column_width(line_index, end[1])
            start_x = max(
                0, min(strip.cell_length, start_column - int(scroll_x) + gutter)
            )
            end_x = max(0, min(strip.cell_length, end_column - int(scroll_x) + gutter))
            if end_x <= start_x:
                continue

            style = (
                self._current_match_style
                if index == self._search_current
                else self._match_style
            )
            cuts = [start_x, end_x]
            if end_x < strip.cell_length:
                cuts.append(strip.cell_length)
            parts = list(strip.divide(cuts))
            if len(parts) < 2:
                continue
            parts[1] = Strip(
                Segment.apply_style(parts[1]._segments, post_style=style),
                parts[1].cell_length,
            )
            strip = Strip.join(parts)

        return strip


# ---------------------------------------------------------------------------
# Small clickable widgets
# ---------------------------------------------------------------------------


class TabItem(Horizontal):
    """A vertical sidebar tab."""

    class Activated(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    class Closed(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(
        self,
        index: int,
        name: str,
        modified: bool,
        active: bool,
        tooltip: str,
    ) -> None:
        super().__init__(classes="tab-item")
        self.tooltip = tooltip
        self.index = index
        self.doc_name = name
        self.modified = modified
        if active:
            self.add_class("active")

    def compose(self) -> ComposeResult:
        yield Static(self._label(), classes="tab-name")
        yield Static("✕", classes="tab-close")

    def _label(self) -> str:
        return ("● " if self.modified else "") + self.doc_name

    def set_name(self, name: str) -> None:
        self.doc_name = name
        self.query_one(".tab-name", Static).update(self._label())

    def set_modified(self, modified: bool) -> None:
        if modified == self.modified:
            return
        self.modified = modified
        self.query_one(".tab-name", Static).update(self._label())

    def set_active(self, active: bool) -> None:
        self.set_class(active, "active")

    def on_click(self, event: events.Click) -> None:
        if event.widget is not None and event.widget.has_class("tab-close"):
            self.post_message(self.Closed(self.index))
        else:
            self.post_message(self.Activated(self.index))


class OpenHint(Static):
    """The splash-screen hint; clicking it opens the file picker."""

    def on_click(self) -> None:
        screen = self.screen
        action = getattr(screen, "action_open_file", None)
        if action is not None:
            action()


class CommandItem(Horizontal):
    """A clickable command row used by the command palette."""

    class Chosen(Message):
        def __init__(self, command_id: str) -> None:
            super().__init__()
            self.command_id = command_id

    def __init__(self, command_id: str, title: str, key: str) -> None:
        super().__init__(classes="command-item")
        self.command_id = command_id
        self.title_text = title
        self.key_text = key

    def compose(self) -> ComposeResult:
        yield Static(self.title_text, classes="command-title")
        yield Static(self.key_text, classes="command-key")

    def on_click(self) -> None:
        self.post_message(self.Chosen(self.command_id))


class TreeRow(Static):
    """One row in the quick-open file tree."""

    class Pressed(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(self, index: int, name: str, is_dir: bool) -> None:
        label = f"▸ {name}" if is_dir else name
        super().__init__(
            Text(label), classes=f"tree-row {'dir' if is_dir else 'file'}"
        )
        self.index = index
        self.entry_name = name
        self.is_dir = is_dir

    def set_expanded(self, expanded: bool) -> None:
        if self.is_dir:
            arrow = "▾" if expanded else "▸"
            self.update(Text(f"{arrow} {self.entry_name}"))

    def set_flat(self, flat: bool, relative_path: str = "") -> None:
        if not self.is_dir:
            self.update(Text(relative_path if flat else self.entry_name))

    def on_click(self) -> None:
        self.post_message(self.Pressed(self.index))


# ---------------------------------------------------------------------------
# Command palette
# ---------------------------------------------------------------------------


class CommandPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, commands: list[tuple[str, str, str]]) -> None:
        super().__init__()
        self.commands = commands
        self.items: list[CommandItem] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="palette", classes="dialog"):
            yield Static("Command Palette", classes="dialog-title")
            yield Input(
                placeholder="Type a command...",
                id="palette-input",
                classes="dialog-input",
            )
            with VerticalScroll(id="palette-list"):
                for command_id, title, key in self.commands:
                    item = CommandItem(command_id, title, key)
                    self.items.append(item)
                    yield item
            yield Static("Enter to run  ·  Esc to close", classes="dialog-hint")

    def on_mount(self) -> None:
        self.query_one("#palette-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "palette-input":
            return
        query = event.value.strip().lower()
        for item in self.items:
            haystack = f"{item.title_text} {item.command_id}".lower()
            item.display = not query or query in haystack

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "palette-input":
            return
        visible = [item for item in self.items if item.display]
        if visible:
            self.dismiss(visible[0].command_id)

    def on_command_item_chosen(self, message: CommandItem.Chosen) -> None:
        self.dismiss(message.command_id)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Quick-open file tree
# ---------------------------------------------------------------------------

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "venv",
    ".venv",
    "env",
    ".env",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".idea",
    "dist",
    "build",
    "target",
    ".next",
    ".turbo",
    "coverage",
}

_SKIP_FILES = {".DS_Store"}

_MAX_TREE_ENTRIES = 4000


@dataclass
class TreeEntry:
    path: Path
    relative: str
    is_dir: bool
    depth: int
    ancestors: tuple[int, ...]


class FileTreeScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(
        self,
        root: Path | None = None,
        api: WorkspaceAPI | None = None,
    ) -> None:
        super().__init__()
        self.root = (root or Path.cwd()).resolve()
        self.api = api or WorkspaceAPI(self.root)
        self.entries: list[TreeEntry] = []
        self.rows: list[TreeRow] = []
        self.expanded: set[int] = set()
        self.filtered = False
        self.truncated = False
        self.query_text = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="filetree", classes="dialog"):
            yield Static("Open File", classes="dialog-title")
            yield Input(
                placeholder="Type to filter files...",
                id="file-filter",
                classes="dialog-input",
            )
            with Horizontal(id="file-actions"):
                yield Button("+ File", id="tree-new-file")
                yield Button("+ Folder", id="tree-new-folder")
                yield Button("Refresh", id="tree-refresh")
                yield Button("Close", id="tree-close")
            yield VerticalScroll(id="file-list")
            yield Static("", id="tree-status", classes="dialog-hint")

    async def on_mount(self) -> None:
        await self._reload(reset_expanded=True)
        self.query_one("#file-filter", Input).focus()

    async def _reload(self, *, reset_expanded: bool = False) -> None:
        if reset_expanded:
            self.expanded.clear()
            self.truncated = False
            self._scan()
        container = self.query_one("#file-list", VerticalScroll)
        await container.remove_children()
        rows: list[TreeRow] = []
        for index, entry in enumerate(self.entries):
            row = TreeRow(index, entry.path.name, entry.is_dir)
            row.styles.padding = (0, 0, 0, 1 + entry.depth * 2)
            rows.append(row)
        self.rows = rows
        if rows:
            await container.mount_all(rows)
        self._update_visibility()

    # -- scanning -------------------------------------------------------

    def _scan(self) -> None:
        entries: list[TreeEntry] = []

        def walk(directory: Path, depth: int, ancestors: tuple[int, ...]) -> None:
            if len(entries) >= _MAX_TREE_ENTRIES:
                self.truncated = True
                return
            try:
                children = sorted(
                    directory.iterdir(),
                    key=lambda path: (not path.is_dir(), path.name.lower()),
                )
            except OSError:
                return

            for path in children:
                if len(entries) >= _MAX_TREE_ENTRIES:
                    self.truncated = True
                    return
                try:
                    is_dir = path.is_dir()
                except OSError:
                    continue

                if path.is_symlink():
                    continue
                if is_dir and (
                    path.name in _SKIP_DIRS or path.name.startswith(".")
                ):
                    continue
                if not is_dir and path.name in _SKIP_FILES:
                    continue

                index = len(entries)
                try:
                    relative = path.relative_to(self.root).as_posix()
                except ValueError:
                    relative = path.name
                entries.append(
                    TreeEntry(path, relative, is_dir, depth, ancestors)
                )
                if is_dir:
                    walk(path, depth + 1, ancestors + (index,))

        walk(self.root, 0, ())
        self.entries = entries

    # -- visibility -----------------------------------------------------

    def _update_visibility(self) -> None:
        self.query_text = self.query_one("#file-filter", Input).value.strip().lower()
        is_filtering = bool(self.query_text)

        if is_filtering != self.filtered:
            self.filtered = is_filtering
            for entry, row in zip(self.entries, self.rows):
                if not entry.is_dir:
                    row.set_flat(is_filtering, entry.relative)

        matches = 0
        for index, (entry, row) in enumerate(zip(self.entries, self.rows)):
            if is_filtering:
                visible = (
                    not entry.is_dir
                    and self.query_text in entry.relative.lower()
                )
                if visible:
                    matches += 1
                    if matches > 300:
                        visible = False
                row.styles.padding = (0, 0, 0, 1)
            else:
                visible = all(
                    ancestor in self.expanded for ancestor in entry.ancestors
                )
                row.styles.padding = (0, 0, 0, 1 + entry.depth * 2)
                if entry.is_dir:
                    row.set_expanded(index in self.expanded)
            row.display = visible

        status = self.query_one("#tree-status", Static)
        if is_filtering:
            if matches == 0:
                status.update("No matching files")
            else:
                status.update(f"{matches} matching file(s)  ·  Enter opens the first")
        else:
            files = sum(1 for entry in self.entries if not entry.is_dir)
            suffix = " (list truncated)" if self.truncated else ""
            status.update(f"{self.root}  ·  {files} files{suffix}")

    # -- interaction ----------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "file-filter":
            self._update_visibility()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "file-filter":
            return
        for index, row in enumerate(self.rows):
            if row.display and not self.entries[index].is_dir:
                self.dismiss(str(self.entries[index].path))
                return

    def on_tree_row_pressed(self, message: TreeRow.Pressed) -> None:
        if not 0 <= message.index < len(self.entries):
            return
        entry = self.entries[message.index]
        if entry.is_dir:
            if message.index in self.expanded:
                self.expanded.remove(message.index)
            else:
                self.expanded.add(message.index)
            self._update_visibility()
        else:
            self.dismiss(str(entry.path))

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "tree-new-file":
            self.app.push_screen(
                TextPromptScreen(
                    "New file",
                    "relative/path/file.py",
                    "",
                    "Create",
                ),
                callback=self._create_file_result,
            )
        elif button_id == "tree-new-folder":
            self.app.push_screen(
                TextPromptScreen(
                    "New folder",
                    "relative/path/folder",
                    "",
                    "Create",
                ),
                callback=self._create_folder_result,
            )
        elif button_id == "tree-refresh":
            await self._reload(reset_expanded=True)
            self.query_one("#file-filter", Input).focus()
        elif button_id == "tree-close":
            self.dismiss(None)

    def _create_file_result(self, value: str | None) -> None:
        if not value:
            return
        try:
            created = self.api.create_file(value)
        except APIError as error:
            self.app.notify(str(error), severity="error")
            return
        self.dismiss(str(self.api.resolve(created["path"])))

    async def _create_folder_result(self, value: str | None) -> None:
        if not value:
            return
        try:
            self.api.create_folder(value)
        except APIError as error:
            self.app.notify(str(error), severity="error")
            return
        self.query_one("#file-filter", Input).value = ""
        await self._reload(reset_expanded=True)
        self.query_one("#file-filter", Input).focus()
        self.app.notify(f"Created folder: {value}", timeout=2)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Search / replace
# ---------------------------------------------------------------------------


class SearchScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("f3", "next_match", "Next match", show=False),
        Binding("shift+f3", "prev_match", "Previous match", show=False),
    ]

    def __init__(self, editor: CodeEditor, replace: bool = False) -> None:
        super().__init__()
        self.editor = editor
        self.replace_mode = replace
        selected = editor.selected_text
        initial = selected if selected and "\n" not in selected else ""
        self.initial_query = initial
        self.ranges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        self.current = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="search", classes="dialog"):
            yield Static(
                "Replace in File" if self.replace_mode else "Find in File",
                classes="dialog-title",
            )
            yield Input(
                value=self.initial_query,
                placeholder="Find...",
                id="find-input",
                classes="dialog-input",
            )
            with Vertical(id="replace-area"):
                yield Input(
                    placeholder="Replace with...",
                    id="replace-input",
                    classes="dialog-input",
                )
            yield Static("", id="search-status", classes="dialog-hint")
            with Horizontal(id="search-buttons"):
                yield Button("Prev", id="btn-prev")
                yield Button("Next", id="btn-next")
                yield Button("Replace", id="btn-replace")
                yield Button("All", id="btn-replace-all", variant="primary")
                yield Button("Close", id="btn-close")

    def on_mount(self) -> None:
        replace_area = self.query_one("#replace-area", Vertical)
        replace_area.display = self.replace_mode
        self.query_one("#btn-replace", Button).display = self.replace_mode
        self.query_one("#btn-replace-all", Button).display = self.replace_mode
        self.query_one("#find-input", Input).focus()
        self._refresh_matches()

    # -- matching -------------------------------------------------------

    def _query(self) -> str:
        return self.query_one("#find-input", Input).value

    def _compute_ranges(self) -> list[tuple[tuple[int, int], tuple[int, int]]]:
        query = self._query()
        if not query:
            return []
        document = self.editor.document
        ranges: list[tuple[tuple[int, int], tuple[int, int]]] = []
        for match in re.finditer(re.escape(query), self.editor.text, re.IGNORECASE):
            ranges.append(
                (
                    document.get_location_from_index(match.start()),
                    document.get_location_from_index(match.end()),
                )
            )
            if len(ranges) >= 2000:
                break
        return ranges

    def _refresh_matches(self, anchor: int | None = None) -> None:
        self.ranges = self._compute_ranges()
        if not self.ranges:
            self.current = -1
            self.editor.set_search_ranges([], -1)
            self._set_status("No results" if self._query() else "Type to search")
            return

        if anchor is None:
            anchor = self.editor.document.get_index_from_location(
                self.editor.selection.start
            )

        index = 0
        for candidate, (start, _end) in enumerate(self.ranges):
            offset = self.editor.document.get_index_from_location(start)
            if offset >= anchor:
                index = candidate
                break

        self._select(index)

    def _select(self, index: int) -> None:
        if not self.ranges:
            return
        index %= len(self.ranges)
        self.current = index
        start, end = self.ranges[index]
        self.editor.set_search_ranges(self.ranges, index)
        self.editor.selection = (start, end)
        self.editor.scroll_cursor_visible(animate=False)
        self._set_status(f"{index + 1} of {len(self.ranges)}")

    def _set_status(self, text: str) -> None:
        self.query_one("#search-status", Static).update(text)

    # -- actions --------------------------------------------------------

    def action_next_match(self) -> None:
        if not self.ranges:
            self._refresh_matches()
        elif self.current >= 0:
            self._select(self.current + 1)

    def action_prev_match(self) -> None:
        if not self.ranges:
            self._refresh_matches()
        elif self.current >= 0:
            self._select(self.current - 1)

    def _replace_current(self) -> None:
        query = self._query()
        if not query:
            return
        if not self.ranges:
            self._refresh_matches()
        if not self.ranges:
            return
        replacement = self.query_one("#replace-input", Input).value
        start, end = self.ranges[self.current]
        self.editor.replace(replacement, start, end)
        self._refresh_matches(
            anchor=self.editor.document.get_index_from_location(
                self.editor.selection.start
            )
        )

    def _replace_all(self) -> None:
        if not self.ranges:
            self._refresh_matches()
        if not self.ranges:
            return
        replacement = self.query_one("#replace-input", Input).value
        count = len(self.ranges)
        for start, end in reversed(self.ranges):
            self.editor.replace(replacement, start, end)
        self._refresh_matches(anchor=0)
        self._set_status(f"Replaced {count} occurrence(s)")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "find-input":
            self._refresh_matches()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "find-input":
            self.action_next_match()
        elif event.input.id == "replace-input":
            self._replace_current()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-next":
            self.action_next_match()
        elif button_id == "btn-prev":
            self.action_prev_match()
        elif button_id == "btn-replace":
            self._replace_current()
        elif button_id == "btn-replace-all":
            self._replace_all()
        elif button_id == "btn-close":
            self.action_close()

    def action_close(self) -> None:
        self.editor.clear_search()
        self.dismiss(None)

    def on_unmount(self) -> None:
        if self.editor.is_mounted:
            self.editor.clear_search()


# ---------------------------------------------------------------------------
# Generic prompts
# ---------------------------------------------------------------------------


class TextPromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        placeholder: str,
        initial: str = "",
        submit_label: str = "OK",
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.placeholder = placeholder
        self.initial = initial
        self.submit_label = submit_label

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt", classes="dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Input(
                value=self.initial,
                placeholder=self.placeholder,
                id="prompt-input",
                classes="dialog-input",
            )
            with Horizontal(id="prompt-buttons"):
                yield Button("Cancel", id="prompt-cancel")
                yield Button(
                    self.submit_label, id="prompt-ok", variant="primary"
                )

    def on_mount(self) -> None:
        self.query_one("#prompt-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        self.dismiss(value or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-ok":
            value = self.query_one("#prompt-input", Input).value.strip()
            self.dismiss(value or None)
        elif event.button.id == "prompt-cancel":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmCloseScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, document_name: str) -> None:
        super().__init__()
        self.document_name = document_name

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="dialog"):
            yield Static("Unsaved changes", classes="dialog-title")
            yield Static(
                f'"{self.document_name}" has unsaved changes.\n'
                "Do you want to save before closing?",
                id="confirm-text",
            )
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="confirm-cancel")
                yield Button("Don't Save", id="confirm-discard")
                yield Button("Save", id="confirm-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#confirm-save", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        mapping = {
            "confirm-save": "save",
            "confirm-discard": "discard",
            "confirm-cancel": "cancel",
        }
        result = mapping.get(event.button.id or "")
        if result is not None:
            self.dismiss(result)

    def action_cancel(self) -> None:
        self.dismiss("cancel")


class ConfirmActionScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        title: str,
        message: str,
        confirm_label: str = "Delete",
    ) -> None:
        super().__init__()
        self.dialog_title = title
        self.message = message
        self.confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="dialog"):
            yield Static(self.dialog_title, classes="dialog-title")
            yield Static(self.message, id="confirm-text")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="action-cancel")
                yield Button(
                    self.confirm_label,
                    id="action-confirm",
                    variant="error",
                )

    def on_mount(self) -> None:
        self.query_one("#action-cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-confirm":
            self.dismiss(True)
        elif event.button.id == "action-cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


COMMANDS: list[tuple[str, str, str]] = [
    ("new_file", "New File", "Ctrl+N"),
    ("new_file_path", "New File on Disk...", "Ctrl+Alt+N"),
    ("new_folder", "New Folder...", "Ctrl+Shift+N"),
    ("open_file", "Open File...", "Ctrl+P"),
    ("save", "Save", "Ctrl+S"),
    ("save_as", "Save As...", "Ctrl+Shift+S"),
    ("save_all", "Save All", "Ctrl+Alt+S"),
    ("reload_current", "Reload from Disk", "F5"),
    ("rename_current", "Rename Current File...", "F2"),
    ("duplicate_current", "Duplicate Current File", ""),
    ("delete_current", "Delete Current File...", "Ctrl+Alt+D"),
    ("close_tab", "Close Tab", "Ctrl+W"),
    ("close_all", "Close All", "Ctrl+Alt+W"),
    ("next_tab", "Next Tab", "Ctrl+Tab"),
    ("toggle_sidebar", "Toggle Sidebar", "Ctrl+B"),
    ("toggle_word_wrap", "Toggle Word Wrap", "Alt+Z"),
    ("find", "Find in File", "Ctrl+F"),
    ("replace", "Replace in File", "Ctrl+H"),
    ("goto_line", "Go to Line", "Ctrl+G"),
    ("delete_line", "Delete Line", "Ctrl+Shift+K"),
    ("duplicate_line", "Duplicate Line", "Ctrl+Shift+D"),
    ("move_line_up", "Move Line Up", "Alt+Up"),
    ("move_line_down", "Move Line Down", "Alt+Down"),
    ("toggle_comment", "Toggle Line Comment", "Ctrl+/"),
    ("select_next_occurrence", "Select Next Occurrence", "Ctrl+D"),
    ("undo", "Undo", "Ctrl+Z"),
    ("redo", "Redo", "Ctrl+Y"),
    ("api_info", "API: Connection Info", ""),
    ("copy_api_token", "API: Copy Token", ""),
    ("command_palette", "Command Palette", "Ctrl+Shift+P / F1"),
    ("quit", "Quit DeepCode", "Ctrl+Q"),
]

_DOC_COMMANDS = {
    "save",
    "save_as",
    "save_all",
    "reload_current",
    "rename_current",
    "duplicate_current",
    "delete_current",
    "close_tab",
    "close_all",
    "toggle_word_wrap",
    "find",
    "replace",
    "goto_line",
    "delete_line",
    "duplicate_line",
    "move_line_up",
    "move_line_down",
    "toggle_comment",
    "select_next_occurrence",
    "undo",
    "redo",
}

_COMMENT_PREFIXES = {
    ".py": "# ",
    ".pyw": "# ",
    ".pyi": "# ",
    ".sh": "# ",
    ".bash": "# ",
    ".zsh": "# ",
    ".fish": "# ",
    ".yaml": "# ",
    ".yml": "# ",
    ".toml": "# ",
    ".rb": "# ",
    ".pl": "# ",
    ".r": "# ",
    ".ini": "# ",
    ".cfg": "# ",
    ".conf": "# ",
    ".go": "// ",
    ".rs": "// ",
    ".js": "// ",
    ".jsx": "// ",
    ".ts": "// ",
    ".tsx": "// ",
    ".java": "// ",
    ".kt": "// ",
    ".kts": "// ",
    ".c": "// ",
    ".h": "// ",
    ".cc": "// ",
    ".cpp": "// ",
    ".cxx": "// ",
    ".hpp": "// ",
    ".cs": "// ",
    ".swift": "// ",
    ".scala": "// ",
    ".php": "// ",
    ".jsonc": "// ",
    ".sql": "-- ",
    ".lua": "-- ",
    ".hs": "-- ",
}

_BLOCK_COMMENT_MARKERS = {
    ".css": ("/* ", " */"),
    ".tcss": ("/* ", " */"),
    ".scss": ("/* ", " */"),
    ".sass": ("/* ", " */"),
    ".less": ("/* ", " */"),
    ".html": ("<!-- ", " -->"),
    ".htm": ("<!-- ", " -->"),
    ".xml": ("<!-- ", " -->"),
    ".svg": ("<!-- ", " -->"),
    ".vue": ("<!-- ", " -->"),
    ".svelte": ("<!-- ", " -->"),
}


class WelcomePanel(Vertical):
    can_focus = True


class ScreenAPIBridge:
    """Thread-safe bridge between the HTTP server and the Textual app."""

    def __init__(self, screen: "MainScreen") -> None:
        self.screen = screen

    def _call(self, callback, *args):
        return self.screen.app.call_from_thread(callback, *args)

    def snapshot(self) -> dict[str, Any]:
        return self._call(self.screen.api_snapshot_ui)

    def open(self, path: str) -> dict[str, Any]:
        return self._call(self.screen.api_open_ui, path)

    def save(self, all_tabs: bool = False) -> dict[str, Any]:
        return self._call(self.screen.api_save_ui, all_tabs)

    def command(self, command_id: str) -> dict[str, Any]:
        return self._call(self.screen.api_command_ui, command_id)


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding(
            "ctrl+shift+p", "command_palette", "Command Palette", priority=True
        ),
        Binding("f1", "command_palette", "Command Palette", priority=True, show=False),
        Binding("ctrl+p", "open_file", "Open File", priority=True),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+shift+s", "save_as", "Save As", priority=True, show=False),
        Binding("ctrl+n", "new_file", "New File", priority=True),
        Binding(
            "ctrl+alt+n", "new_file_path", "New File on Disk", priority=True, show=False
        ),
        Binding(
            "ctrl+shift+n", "new_folder", "New Folder", priority=True, show=False
        ),
        Binding("ctrl+w", "close_tab", "Close Tab", priority=True),
        Binding(
            "ctrl+alt+w", "close_all", "Close All", priority=True, show=False
        ),
        Binding(
            "ctrl+alt+s", "save_all", "Save All", priority=True, show=False
        ),
        Binding("f5", "reload_current", "Reload", priority=True, show=False),
        Binding("f2", "rename_current", "Rename", priority=True, show=False),
        Binding(
            "ctrl+alt+d", "delete_current", "Delete File", priority=True, show=False
        ),
        Binding(
            "ctrl+tab", "next_tab", "Next Tab", priority=True, show=False
        ),
        Binding(
            "ctrl+pageup", "prev_tab", "Previous Tab", priority=True, show=False
        ),
        Binding(
            "ctrl+pagedown", "next_tab", "Next Tab", priority=True, show=False
        ),
        Binding("ctrl+b", "toggle_sidebar", "Toggle Sidebar", priority=True),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("ctrl+h", "replace", "Replace", priority=True),
        Binding("ctrl+g", "goto_line", "Go to Line", priority=True),
        Binding(
            "ctrl+shift+k", "delete_line", "Delete Line", priority=True, show=False
        ),
        Binding(
            "ctrl+shift+d", "duplicate_line", "Duplicate Line", priority=True, show=False
        ),
        Binding(
            "alt+z", "toggle_word_wrap", "Toggle Word Wrap", priority=True, show=False
        ),
        Binding(
            "alt+up", "move_line_up", "Move Line Up", priority=True, show=False
        ),
        Binding(
            "alt+down", "move_line_down", "Move Line Down", priority=True, show=False
        ),
        Binding(
            "ctrl+/", "toggle_comment", "Toggle Comment", priority=True, show=False
        ),
        Binding(
            "ctrl+d",
            "select_next_occurrence",
            "Select Next Occurrence",
            priority=True,
            show=False,
        ),
        Binding("ctrl+z", "undo", "Undo", priority=True, show=False),
        Binding("ctrl+y", "redo", "Redo", priority=True, show=False),
        Binding("ctrl+q", "quit", "Quit", priority=True, show=False),
    ]

    def __init__(
        self,
        workspace: Path | None = None,
        api_enabled: bool = True,
        api_port: int = 8765,
        initial_file: Path | None = None,
    ) -> None:
        super().__init__()
        self.workspace = (workspace or Path.cwd()).resolve()
        self.workspace_api = WorkspaceAPI(self.workspace)
        self.api_enabled = api_enabled
        self.api_port = api_port
        self.initial_file = initial_file
        self.api_server: LocalAPIServer | None = None
        self.api_info: dict[str, Any] | None = None
        self.documents: list[Document] = []
        self.active_index = -1
        self._loading = False

    # -- composition ----------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("OPEN FILES", id="sidebar-title")
                yield VerticalScroll(id="tabs")
            with Vertical(id="editor-area"):
                with WelcomePanel(id="welcome"):
                    yield Static(LOGO, id="logo")
                    yield OpenHint(
                        "Press Ctrl+P to open file", id="logo-hint"
                    )
                yield CodeEditor(id="editor")

    def on_mount(self) -> None:
        self.query_one("#welcome", WelcomePanel).focus()
        if self.api_enabled:
            self._start_api_server()
        if self.initial_file is not None:
            self.run_worker(self._open_initial_file())

    def on_unmount(self) -> None:
        self._stop_api_server()

    async def _open_initial_file(self) -> None:
        if self.initial_file is not None:
            await self._open_path(self.initial_file)

    # -- API lifecycle --------------------------------------------------

    def _start_api_server(self) -> None:
        if self.api_server is not None:
            return
        server = LocalAPIServer(
            self.workspace,
            bridge=ScreenAPIBridge(self),
            port=self.api_port,
        )
        try:
            info = server.start()
        except OSError as error:
            self.notify(f"Cannot start API: {error}", severity="error", timeout=6)
            return
        self.api_server = server
        self.api_info = info

    def _stop_api_server(self) -> None:
        if self.api_server is None:
            return
        self.api_server.stop()
        self.api_server = None

    def api_snapshot_ui(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "active_index": self.active_index,
            "sidebar_visible": bool(self.query_one("#sidebar").display),
            "word_wrap": bool(self.editor.soft_wrap),
            "cursor": (
                list(self.editor.cursor_location)
                if self.current_doc is not None
                else None
            ),
            "documents": [
                {
                    "name": document.name,
                    "path": str(document.path) if document.path else None,
                    "modified": document.modified,
                    "active": index == self.active_index,
                }
                for index, document in enumerate(self.documents)
            ],
        }

    async def api_open_ui(self, path: str) -> dict[str, Any]:
        await self._open_path(path)
        document = self.current_doc
        return {
            "opened": document.name if document else None,
            "active_index": self.active_index,
        }

    def api_save_ui(self, all_tabs: bool = False) -> dict[str, Any]:
        if all_tabs:
            return {"saved": self._save_all(silent=True), "all": True}
        document = self.current_doc
        if document is None:
            raise APIError("No active document", 409)
        if document.path is None:
            raise APIError("Active document has no path", 409)
        if not self._write_doc(document, notify=False):
            raise APIError("Save failed", 500)
        return {"saved": document.name, "all": False}

    async def api_command_ui(self, command_id: str) -> dict[str, Any]:
        allowed = {command[0] for command in self._available_commands()}
        allowed -= {"quit", "command_palette"}
        if command_id not in allowed:
            raise APIError(f"Unknown or unavailable command: {command_id}", 400)
        handler = getattr(self, f"action_{command_id}", None)
        if handler is None:
            raise APIError(f"Command is not executable: {command_id}", 400)
        result = handler()
        if inspect.isawaitable(result):
            await result
        return {"command": command_id, "ok": True}

    # -- convenient accessors ------------------------------------------

    @property
    def editor(self) -> CodeEditor:
        return self.query_one("#editor", CodeEditor)

    @property
    def tabs(self) -> VerticalScroll:
        return self.query_one("#tabs", VerticalScroll)

    @property
    def welcome(self) -> WelcomePanel:
        return self.query_one("#welcome", WelcomePanel)

    @property
    def current_doc(self) -> Document | None:
        if 0 <= self.active_index < len(self.documents):
            return self.documents[self.active_index]
        return None

    # -- document lifecycle --------------------------------------------

    def _sync_active(self) -> None:
        doc = self.current_doc
        if doc is None:
            return
        doc.text = self.editor.text
        doc.cursor = self.editor.selection.end
        doc.scroll_y = int(self.editor.scroll_offset[1])

    def _apply_editor_language(self, doc: Document) -> None:
        language = detect_language(doc.path, doc.text)
        try:
            if self.editor.language != language:
                self.editor.language = language
        except LanguageDoesNotExist:
            self.editor.language = None

    def _load_doc(self, doc: Document) -> None:
        self._loading = True
        try:
            self.editor.load_text(doc.text)
            self._apply_editor_language(doc)
            self.editor.selection = (doc.cursor, doc.cursor)
        finally:
            self._loading = False
        self.editor.display = True
        self.welcome.display = False
        self.editor.focus()
        scroll_y = doc.scroll_y
        self.call_after_refresh(
            lambda: self.editor.scroll_to(0, scroll_y, animate=False, immediate=True)
        )

    def _activate_index(self, index: int) -> None:
        if not 0 <= index < len(self.documents):
            return
        if index != self.active_index:
            self._sync_active()
            self.active_index = index
            self._load_doc(self.documents[index])
        else:
            self.editor.focus()
        for tab in self.query(TabItem):
            tab.set_active(tab.index == index)

    async def _rebuild_tabs(self) -> None:
        await self.tabs.remove_children()
        items = [
            TabItem(
                index,
                doc.name,
                doc.modified,
                index == self.active_index,
                str(doc.path) if doc.path is not None else "Unsaved file",
            )
            for index, doc in enumerate(self.documents)
        ]
        if items:
            await self.tabs.mount_all(items)

    def _find_tab(self, index: int) -> TabItem | None:
        for tab in self.query(TabItem):
            if tab.index == index:
                return tab
        return None

    def _doc_index(self, document: Document) -> int:
        for index, candidate in enumerate(self.documents):
            if candidate is document:
                return index
        return -1

    def _update_modified(self, index: int, modified: bool) -> None:
        tab = self._find_tab(index)
        if tab is not None:
            tab.set_modified(modified)

    async def _open_path(self, raw_path: str | Path) -> None:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        try:
            path = path.resolve()
        except OSError:
            pass

        if not path.is_file():
            self.notify(f"Not a file: {path}", severity="error")
            return

        for index, doc in enumerate(self.documents):
            if doc.path is not None and doc.path == path:
                self._activate_index(index)
                return

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as error:
                self.notify(f"Cannot open {path.name}: {error}", severity="error")
                return
        except OSError as error:
            self.notify(f"Cannot open {path.name}: {error}", severity="error")
            return

        self._sync_active()
        document = Document(
            name=path.name,
            path=path,
            text=text,
            saved_text=text,
        )
        self.documents.append(document)
        self.active_index = len(self.documents) - 1
        self._load_doc(document)
        await self._rebuild_tabs()

    async def _close_index(self, index: int) -> None:
        if not 0 <= index < len(self.documents):
            return
        if index == self.active_index:
            self._sync_active()
        was_active = index == self.active_index
        del self.documents[index]

        if not self.documents:
            self.active_index = -1
            self.editor.display = False
            self._loading = True
            try:
                self.editor.load_text("")
                self.editor.language = None
            finally:
                self._loading = False
            self.welcome.display = True
            self.welcome.focus()
        elif was_active:
            self.active_index = min(index, len(self.documents) - 1)
            self._load_doc(self.documents[self.active_index])
        elif index < self.active_index:
            self.active_index -= 1
        else:
            self.editor.focus()

        await self._rebuild_tabs()

    async def _request_close(self, index: int) -> None:
        if not 0 <= index < len(self.documents):
            return
        document = self.documents[index]
        if document.modified:
            self.app.push_screen(
                ConfirmCloseScreen(document.name),
                callback=partial(self._handle_confirm_close, index),
            )
        else:
            await self._close_index(index)

    async def _handle_confirm_close(self, index: int, result: str) -> None:
        if not 0 <= index < len(self.documents):
            return
        if result == "discard":
            await self._close_index(index)
        elif result == "save":
            document = self.documents[index]
            if document.path is None:
                self.app.push_screen(
                    TextPromptScreen(
                        "Save file as",
                        "path/to/file.py",
                        str(Path.cwd() / document.name),
                        "Save",
                    ),
                    callback=partial(self._save_as_then_close, index),
                )
            else:
                if self._write_doc(document):
                    await self._close_index(index)
        else:
            self._modal_closed(None)

    async def _save_as_then_close(self, index: int, value: str | None) -> None:
        if not value or not 0 <= index < len(self.documents):
            self._modal_closed(None)
            return
        document = self.documents[index]
        if document is self.current_doc:
            text = self.editor.text
        else:
            text = document.text
        path = self._normalise_path(value)
        if path is None:
            return
        document.path = path
        document.name = path.name
        if self._write_doc(document, text):
            await self._close_index(index)

    # -- saving ---------------------------------------------------------

    def _normalise_path(self, value: str) -> Path | None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        try:
            return path.resolve()
        except OSError:
            return path

    def _write_doc(
        self,
        document: Document,
        text: str | None = None,
        *,
        notify: bool = True,
    ) -> bool:
        if document.path is None:
            return False
        if text is None:
            text = self.editor.text if document is self.current_doc else document.text
        try:
            document.path.parent.mkdir(parents=True, exist_ok=True)
            document.path.write_text(text, encoding="utf-8")
        except OSError as error:
            if notify:
                self.notify(
                    f"Cannot save {document.name}: {error}", severity="error"
                )
            return False

        document.text = text
        document.saved_text = text
        document.modified = False
        if document is self.current_doc:
            document.cursor = self.editor.selection.end

        index = next(
            (
                candidate_index
                for candidate_index, candidate in enumerate(self.documents)
                if candidate is document
            ),
            -1,
        )
        tab = self._find_tab(index) if index >= 0 else None
        if tab is not None:
            tab.set_name(document.name)
            tab.set_modified(False)
            tab.tooltip = str(document.path)
        if notify:
            self.notify(f"Saved {document.name}", timeout=1.5)
        return True

    def _save_all(self, *, silent: bool = False) -> int:
        if not self.documents:
            return 0
        self._sync_active()
        saved = 0
        skipped = 0
        for document in self.documents:
            if document.path is None:
                skipped += 1
                continue
            if self._write_doc(document, notify=False):
                saved += 1
        if not silent:
            message = f"Saved {saved} file(s)"
            if skipped:
                message += f", {skipped} unsaved buffer(s) skipped"
            self.notify(message, timeout=2)
        return saved

    def _modal_closed(self, _result: object = None) -> None:
        if self.current_doc is not None:
            self.editor.focus()
        else:
            self.welcome.focus()

    # -- command helpers ------------------------------------------------

    def _available_commands(self) -> list[tuple[str, str, str]]:
        if self.current_doc is None:
            return [
                command
                for command in COMMANDS
                if command[0] not in _DOC_COMMANDS
            ]
        return list(COMMANDS)

    async def _run_command(self, command_id: str | None) -> None:
        if not command_id:
            self._modal_closed(None)
            return
        handler = getattr(self, f"action_{command_id}", None)
        if handler is None:
            return
        result = handler()
        if inspect.isawaitable(result):
            await result

    def _document_for_editor_action(self) -> Document | None:
        document = self.current_doc
        if document is None:
            self.notify("Open a file first", severity="warning")
        return document

    def _comment_markers(self) -> tuple[str, str]:
        document = self.current_doc
        if document is not None and document.path is not None:
            suffix = document.path.suffix.lower()
            if suffix in _BLOCK_COMMENT_MARKERS:
                return _BLOCK_COMMENT_MARKERS[suffix]
            return _COMMENT_PREFIXES.get(suffix, "# "), ""
        return "# ", ""

    # -- actions --------------------------------------------------------

    def action_command_palette(self) -> None:
        self.app.push_screen(
            CommandPaletteScreen(self._available_commands()),
            callback=self._run_command,
        )

    async def action_new_file(self) -> None:
        self._sync_active()
        used = {document.name for document in self.documents}
        number = 1
        while f"Untitled-{number}" in used:
            number += 1
        document = Document(name=f"Untitled-{number}")
        self.documents.append(document)
        self.active_index = len(self.documents) - 1
        self._load_doc(document)
        await self._rebuild_tabs()

    def action_save(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        if document.path is None:
            self.action_save_as()
            return
        self._write_doc(document)

    def action_save_as(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        initial = (
            str(document.path)
            if document.path is not None
            else str(self.workspace / document.name)
        )
        self.app.push_screen(
            TextPromptScreen(
                "Save file as", "path/to/file.py", initial, "Save"
            ),
            callback=self._save_as_result,
        )

    def _save_as_result(self, value: str | None) -> None:
        document = self.current_doc
        if document is None or not value:
            self._modal_closed(None)
            return
        path = self._normalise_path(value)
        if path is None:
            self._modal_closed(None)
            return
        document.path = path
        document.name = path.name
        text = self.editor.text
        self._write_doc(document, text)
        self._apply_editor_language(document)
        self._modal_closed(None)

    def action_open_file(self) -> None:
        self.app.push_screen(
            FileTreeScreen(self.workspace, self.workspace_api),
            callback=self._open_file_result,
        )

    async def _open_file_result(self, path: str | None) -> None:
        if path:
            await self._open_path(path)
        self._modal_closed(None)

    # -- filesystem actions ---------------------------------------------

    def action_new_file_path(self) -> None:
        self.app.push_screen(
            TextPromptScreen(
                "New file on disk",
                "relative/path/file.py",
                "",
                "Create",
            ),
            callback=self._new_file_path_result,
        )

    async def _new_file_path_result(self, value: str | None) -> None:
        if not value:
            self._modal_closed(None)
            return
        try:
            created = self.workspace_api.create_file(value)
        except APIError as error:
            self.notify(str(error), severity="error", timeout=4)
            self._modal_closed(None)
            return
        path = self.workspace_api.resolve(created["path"])
        await self._open_path(path)
        self._modal_closed(None)

    def action_new_folder(self) -> None:
        self.app.push_screen(
            TextPromptScreen(
                "New folder",
                "relative/path/folder",
                "",
                "Create",
            ),
            callback=self._new_folder_result,
        )

    def _new_folder_result(self, value: str | None) -> None:
        if not value:
            self._modal_closed(None)
            return
        try:
            folder = self.workspace_api.create_folder(value)
        except APIError as error:
            self.notify(str(error), severity="error", timeout=4)
            return
        self.notify(f"Created folder: {folder['path']}", timeout=2)
        self._modal_closed(None)

    def action_rename_current(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        if document.path is None:
            self.notify("Save the buffer before renaming it", severity="warning")
            return
        try:
            initial = self.workspace_api.relative(document.path)
        except APIError:
            initial = str(document.path)
        self.app.push_screen(
            TextPromptScreen(
                "Rename file",
                "new/path/file.py",
                initial,
                "Rename",
            ),
            callback=self._rename_current_result,
        )

    def _rename_current_result(self, value: str | None) -> None:
        document = self.current_doc
        if document is None or document.path is None or not value:
            self._modal_closed(None)
            return
        target = self._normalise_path(value)
        if target is None:
            self._modal_closed(None)
            return
        if target == document.path:
            self._modal_closed(None)
            return
        if target.exists():
            self.notify(f"Target already exists: {target}", severity="error")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            document.path.rename(target)
        except OSError as error:
            self.notify(f"Cannot rename: {error}", severity="error")
            return
        document.path = target
        document.name = target.name
        self._apply_editor_language(document)
        index = self._doc_index(document)
        tab = self._find_tab(index)
        if tab is not None:
            tab.set_name(document.name)
            tab.tooltip = str(document.path)
        self.notify(f"Renamed to {document.name}", timeout=2)
        self._modal_closed(None)

    async def action_duplicate_current(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        if document.path is None:
            self.notify("Save the buffer before duplicating it", severity="warning")
            return
        try:
            relative = self.workspace_api.relative(document.path)
            duplicate = self.workspace_api.duplicate(relative)
        except APIError as error:
            self.notify(str(error), severity="error")
            return
        self.notify(f"Duplicated as {duplicate['name']}", timeout=2)
        await self._open_path(self.workspace_api.resolve(duplicate["path"]))

    async def action_delete_current(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        if document.path is None:
            await self.action_close_tab()
            return
        self.app.push_screen(
            ConfirmActionScreen(
                "Delete file",
                f'Permanently delete "{document.name}"?\n'
                "This cannot be undone.",
                "Delete",
            ),
            callback=partial(self._delete_current_result, document),
        )

    async def _delete_current_result(
        self, document: Document, confirmed: bool
    ) -> None:
        if not confirmed or self._doc_index(document) < 0:
            self._modal_closed(None)
            return
        path = document.path
        if path is None:
            return
        try:
            try:
                relative = self.workspace_api.relative(path)
            except APIError:
                path.unlink()
            else:
                self.workspace_api.delete(relative)
        except (OSError, APIError) as error:
            self.notify(f"Cannot delete: {error}", severity="error")
            self._modal_closed(None)
            return
        index = self._doc_index(document)
        self.notify(f"Deleted {document.name}", timeout=2)
        await self._close_index(index)

    def action_save_all(self) -> None:
        if not self.documents:
            return
        self._save_all()

    def action_toggle_word_wrap(self) -> None:
        if self.current_doc is None:
            return
        self.editor.soft_wrap = not self.editor.soft_wrap
        self.editor.scroll_cursor_visible()
        state = "on" if self.editor.soft_wrap else "off"
        self.notify(f"Word wrap: {state}", timeout=1.5)

    def action_duplicate_line(self) -> None:
        if self._document_for_editor_action() is None:
            return
        editor = self.editor
        start, end = editor.selection
        start_row = start[0]
        end_row = end[0]
        if end[1] == 0 and end_row > start_row:
            end_row -= 1
        lines = [
            editor.document.get_line(row)
            for row in range(start_row, end_row + 1)
        ]
        block = "\n".join(lines)
        insert_at = (end_row, len(lines[-1]))
        editor.insert("\n" + block, insert_at)
        editor.move_cursor((end_row + 1, start[1]))
        editor.scroll_cursor_visible()
        editor.focus()

    def action_reload_current(self) -> None:
        document = self._document_for_editor_action()
        if document is None:
            return
        if document.path is None:
            self.notify("This buffer has no file on disk", severity="warning")
            return
        if document.modified:
            self.app.push_screen(
                ConfirmActionScreen(
                    "Reload from disk",
                    f'Discard unsaved changes in "{document.name}" and reload?',
                    "Reload",
                ),
                callback=partial(self._reload_current_result, document),
            )
        else:
            self._reload_document(document)

    def _reload_current_result(self, document: Document, confirmed: bool) -> None:
        if confirmed:
            self._reload_document(document)
        else:
            self._modal_closed(None)

    def _reload_document(self, document: Document) -> None:
        if document.path is None:
            return
        try:
            text = document.path.read_text(encoding="utf-8", errors="replace")
        except OSError as error:
            self.notify(f"Cannot reload: {error}", severity="error")
            return
        document.text = text
        document.saved_text = text
        document.modified = False
        if document is self.current_doc:
            self._loading = True
            try:
                self.editor.load_text(text)
                self._apply_editor_language(document)
                cursor = document.cursor
                self.editor.selection = (cursor, cursor)
            finally:
                self._loading = False
            self.editor.focus()
        index = self._doc_index(document)
        self._update_modified(index, False)
        self.notify(f"Reloaded {document.name}", timeout=2)

    async def action_close_all(self) -> None:
        if not self.documents:
            return
        if any(document.modified for document in self.documents):
            self.app.push_screen(
                ConfirmActionScreen(
                    "Close all tabs",
                    "There are unsaved changes.\n"
                    "Close every tab and discard them?",
                    "Close All",
                ),
                callback=self._close_all_result,
            )
        else:
            await self._close_all()

    async def _close_all_result(self, confirmed: bool) -> None:
        if confirmed:
            await self._close_all()
        else:
            self._modal_closed(None)

    async def _close_all(self) -> None:
        self.documents.clear()
        self.active_index = -1
        self._loading = True
        try:
            self.editor.load_text("")
            self.editor.language = None
        finally:
            self._loading = False
        self.editor.display = False
        self.welcome.display = True
        self.welcome.focus()
        await self._rebuild_tabs()

    def action_api_info(self) -> None:
        if self.api_info is None:
            self.notify("API server is not running", severity="warning")
            return
        self.notify(
            f"URL: {self.api_info['url']}\n"
            f"Token: {self.api_info['token']}\n"
            f"Config: {self.api_info['config']}",
            title="DeepCode API",
            timeout=12,
        )

    def action_copy_api_token(self) -> None:
        if self.api_info is None:
            self.notify("API server is not running", severity="warning")
            return
        self.app.copy_to_clipboard(self.api_info["token"])
        self.notify("API token copied to clipboard", timeout=2)

    async def action_close_tab(self) -> None:
        if self.current_doc is None:
            return
        await self._request_close(self.active_index)

    def action_next_tab(self) -> None:
        if len(self.documents) < 2:
            return
        self._activate_index((self.active_index + 1) % len(self.documents))

    def action_prev_tab(self) -> None:
        if len(self.documents) < 2:
            return
        self._activate_index((self.active_index - 1) % len(self.documents))

    def action_toggle_sidebar(self) -> None:
        sidebar = self.query_one("#sidebar")
        sidebar.display = not sidebar.display

    def action_find(self) -> None:
        if self._document_for_editor_action() is None:
            return
        self.app.push_screen(
            SearchScreen(self.editor, replace=False),
            callback=self._modal_closed,
        )

    def action_replace(self) -> None:
        if self._document_for_editor_action() is None:
            return
        self.app.push_screen(
            SearchScreen(self.editor, replace=True),
            callback=self._modal_closed,
        )

    def action_goto_line(self) -> None:
        if self._document_for_editor_action() is None:
            return
        self.app.push_screen(
            TextPromptScreen(
                "Go to line", "Line number", "", "Go"
            ),
            callback=self._goto_line_result,
        )

    def _goto_line_result(self, value: str | None) -> None:
        document = self.current_doc
        if document is None or not value:
            self._modal_closed(None)
            return
        try:
            line = int(value)
        except ValueError:
            self.notify("Enter a valid line number", severity="error")
            self._modal_closed(None)
            return
        line_count = self.editor.document.line_count
        line = max(1, min(line, line_count))
        self.editor.move_cursor((line - 1, 0), center=True)
        self.editor.focus()

    def action_delete_line(self) -> None:
        if self._document_for_editor_action() is None:
            return
        self.editor.action_delete_line()
        self.editor.focus()

    def _move_line(self, delta: int) -> None:
        if self._document_for_editor_action() is None:
            return
        editor = self.editor
        row, column = editor.cursor_location
        line_count = editor.document.line_count
        target_row = row + delta
        if target_row < 0 or target_row >= line_count:
            return

        if delta < 0:
            previous = editor.document.get_line(row - 1)
            current = editor.document.get_line(row)
            start = (row - 1, 0)
            end = (row, len(current))
            replacement = current + "\n" + previous
            new_column = min(column, len(current))
            new_row = row - 1
        else:
            current = editor.document.get_line(row)
            following = editor.document.get_line(row + 1)
            start = (row, 0)
            end = (row + 1, len(following))
            replacement = following + "\n" + current
            new_column = min(column, len(current))
            new_row = row + 1

        editor.replace(replacement, start, end)
        editor.move_cursor((new_row, new_column))
        editor.scroll_cursor_visible()
        editor.focus()

    def action_move_line_up(self) -> None:
        self._move_line(-1)

    def action_move_line_down(self) -> None:
        self._move_line(1)

    def action_toggle_comment(self) -> None:
        if self._document_for_editor_action() is None:
            return
        editor = self.editor
        selection_start, selection_end = editor.selection
        start_row, _start_column = selection_start
        end_row, end_column = selection_end
        if end_column == 0 and end_row > start_row:
            end_row -= 1
        end_row = max(start_row, end_row)

        lines = [
            editor.document.get_line(row)
            for row in range(start_row, end_row + 1)
        ]
        non_empty = [line for line in lines if line.strip()]
        prefix, suffix = self._comment_markers()
        start_marker = prefix.strip()
        end_marker = suffix.strip()

        def is_commented(line: str) -> bool:
            stripped = line.lstrip()
            if not stripped.startswith(start_marker):
                return False
            if not end_marker:
                return True
            return stripped.rstrip().endswith(end_marker)

        remove = bool(non_empty) and all(is_commented(line) for line in non_empty)

        new_lines: list[str] = []
        for line in lines:
            if not line.strip():
                new_lines.append(line)
                continue
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            if remove:
                remainder = stripped[len(start_marker) :]
                if remainder.startswith(" "):
                    remainder = remainder[1:]
                if end_marker:
                    trimmed = remainder.rstrip()
                    if trimmed.endswith(end_marker):
                        trimmed = trimmed[: -len(end_marker)]
                    remainder = trimmed.rstrip()
                new_lines.append(indent + remainder)
            else:
                new_lines.append(indent + prefix + stripped + suffix)

        cursor_row, cursor_column = editor.cursor_location
        cursor_line_index = cursor_row - start_row
        if 0 <= cursor_line_index < len(lines):
            old_cursor_line = lines[cursor_line_index]
            new_cursor_line = new_lines[cursor_line_index]
            delta = len(new_cursor_line) - len(old_cursor_line)
        else:
            delta = 0

        replacement = "\n".join(new_lines)
        start = (start_row, 0)
        end = (end_row, len(lines[-1]))
        editor.replace(replacement, start, end)
        if 0 <= cursor_line_index < len(new_lines):
            new_column = max(
                0,
                min(
                    len(new_lines[cursor_line_index]),
                    cursor_column + delta,
                ),
            )
            editor.move_cursor((cursor_row, new_column))
        editor.focus()

    def action_select_next_occurrence(self) -> None:
        if self._document_for_editor_action() is None:
            return
        editor = self.editor
        start, end = editor.selection
        selected_text = editor.selected_text

        if start == end or not selected_text:
            row, column = editor.cursor_location
            line = editor.document.get_line(row)
            column = min(column, len(line))
            left_match = re.search(r"\w*$", line[:column])
            right_match = re.match(r"\w*", line[column:])
            left = left_match.group(0) if left_match else ""
            right = right_match.group(0) if right_match else ""
            word = left + right
            if not word:
                return
            word_start = (row, column - len(left))
            word_end = (row, column + len(right))
            editor.selection = (word_start, word_end)
            editor.scroll_cursor_visible()
            return

        query = selected_text
        text = editor.text
        current_start = editor.document.get_index_from_location(start)
        start_offset = editor.document.get_index_from_location(end)
        found = text.find(query, start_offset)
        if found < 0:
            wrapped = text.find(query, 0, start_offset)
            if wrapped < 0 or wrapped == current_start:
                return
            found = wrapped
        new_start = editor.document.get_location_from_index(found)
        new_end = editor.document.get_location_from_index(found + len(query))
        editor.selection = (new_start, new_end)
        editor.scroll_cursor_visible()

    def action_undo(self) -> None:
        if self.current_doc is None:
            return
        self.editor.undo()
        self.editor.focus()

    def action_redo(self) -> None:
        if self.current_doc is None:
            return
        self.editor.redo()
        self.editor.focus()

    def action_quit(self) -> None:
        self.app.exit()

    # -- messages -------------------------------------------------------

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self.editor:
            return
        document = self.current_doc
        if document is None:
            return
        document.text = event.text_area.text
        modified = document.text != document.saved_text
        if modified != document.modified:
            document.modified = modified
            self._update_modified(self.active_index, modified)

    def on_tab_item_activated(self, message: TabItem.Activated) -> None:
        self._activate_index(message.index)

    @on(TabItem.Closed)
    async def _handle_tab_close(self, message: TabItem.Closed) -> None:
        await self._request_close(message.index)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class DeepCodeApp(App[None]):
    CSS_PATH = "style.tcss"
    TITLE = "DeepCode"
    SUB_TITLE = "TUI code editor"
    ENABLE_COMMAND_PALETTE = False

    # Textual enables terminal mouse reporting by default in modern releases.
    # The flag is kept so the intent remains explicit and older Textual
    # versions still switch pointer events on.
    mouse_support = True

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        initial_file: Path | None = None,
        api_enabled: bool = True,
        api_port: int = 8765,
    ) -> None:
        super().__init__()
        self.workspace = (workspace or Path.cwd()).resolve()
        self.initial_file = initial_file
        self.api_enabled = api_enabled
        self.api_port = api_port

    def on_mount(self) -> None:
        self.push_screen(
            MainScreen(
                workspace=self.workspace,
                initial_file=self.initial_file,
                api_enabled=self.api_enabled,
                api_port=self.api_port,
            )
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="deepcode",
        description="DeepCode - a small TUI code editor.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="File or directory to open (defaults to the current directory).",
    )
    parser.add_argument(
        "--no-api",
        action="store_true",
        help="Do not start the local HTTP API server.",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=int(os.environ.get("DEEPCODE_API_PORT", "8765")),
        help="Local API port (0 chooses a free port).",
    )
    args = parser.parse_args(argv)

    workspace = Path.cwd()
    initial_file: Path | None = None
    if args.path:
        target = Path(args.path).expanduser().resolve()
        if target.is_dir():
            workspace = target
        elif target.is_file():
            workspace = target.parent
            initial_file = target
        else:
            parser.error(f"Path does not exist: {args.path}")

    workspace = workspace.resolve()
    os.chdir(workspace)

    api_enabled = (
        not args.no_api
        and os.environ.get("DEEPCODE_API", "1").strip().lower()
        not in {"0", "false", "no", "off"}
    )
    app = DeepCodeApp(
        workspace=workspace,
        initial_file=initial_file,
        api_enabled=api_enabled,
        api_port=args.api_port,
    )
    app.run()


if __name__ == "__main__":
    main()
