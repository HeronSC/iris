# File: core/tests/test_results_html.py

"""Native rendering of the result contract (6): every kind becomes HTML a client can show as-is."""

from __future__ import annotations

import unittest

from core.results.html import (
    CANCEL_URL,
    CONFIRM_URL,
    MAX_TABLE_ROWS,
    diff_lines_html,
    open_url,
    render_approval_bar,
    render_confirmation,
    render_page,
    render_result,
    render_results,
)
from core.results.models import Source, chart, code, diff, file, image, link, status, table, text, video

SOURCE = Source("weather", "capability", "https://api.example.com/weather")
REPORT_PATH = "E:\\Docs\\report.txt"
NOTES_PATH = "E:\\Docs\\notes.txt"
DOCS_FOLDER = "E:\\Docs"
SHOT_PATH = "E:\\Shots\\cam 1.png"
FILE_SOURCE = Source("disk_usage", "tool", REPORT_PATH)


class BodyTests(unittest.TestCase):
    def test_every_kind_renders_its_own_markup(self) -> None:
        cases = [
            (text("Rain **today**", source=SOURCE), "<p>Rain **today**</p>"),
            (table(("city", "temp"), [("Boston", 48)], source=SOURCE), '<td class="num">48</td>'),
            (image(mime_type="image/png", base64="AA==", source=SOURCE, alt="radar"), 'src="data:image/png;base64,AA=="'),
            (video("https://example.com/clip.mp4", source=SOURCE, mime_type="video/mp4"), '<video class="result-video"'),
            (file(NOTES_PATH, source=SOURCE, size_bytes=2048), '<div class="file-name">notes.txt</div>'),
            (code("print('hi')", source=SOURCE, language="python"), '<code class="language-python">print(&#x27;hi&#x27;)</code>'),
            (diff("--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y", source=SOURCE), '<span class="add">+y</span>'),
            (link("https://example.com", source=SOURCE, title="Example", author="A. Writer"), '<a href="https://example.com">Example</a>'),
            (chart("bar", [{"name": "temp", "values": [48, 91]}], labels=("Boston", "Austin"), source=SOURCE), "<svg viewBox"),
            (status("warning", "Two stations did not answer", source=SOURCE, details={"missing": 2}), '<span class="badge badge-warning">Warning</span>'),
        ]
        for result, marker in cases:
            with self.subTest(kind=result.kind.value):
                rendered = render_result(result)
                self.assertIn(marker, rendered)
                self.assertIn(f'class="result result-{result.kind.value}"', rendered)

    def test_text_uses_the_markdown_converter_when_given_one(self) -> None:
        result = text("# Heading", source=SOURCE)
        self.assertIn("<p># Heading</p>", render_result(result))
        self.assertIn("<h1>Heading</h1>", render_result(result, markdown=lambda body: "<h1>Heading</h1>"))
        plain = text("<b>not bold</b>", source=SOURCE, format="plain")
        self.assertIn("&lt;b&gt;not bold&lt;/b&gt;", render_result(plain, markdown=lambda body: "converted"))

    def test_untrusted_text_is_escaped_everywhere(self) -> None:
        nasty = "<script>alert(1)</script>"
        for result in (
            text(nasty, source=SOURCE, format="plain", title=nasty),
            table((nasty,), [(nasty,)], source=SOURCE),
            code(nasty, source=SOURCE, path=nasty),
            status("error", nasty, source=SOURCE, details={nasty: nasty}),
            link("https://example.com/?q=<x>", source=SOURCE, description=nasty),
        ):
            with self.subTest(kind=result.kind.value):
                self.assertNotIn("<script>", render_result(result))

    def test_long_tables_are_cut_and_say_so(self) -> None:
        rows = [(index, index * 2) for index in range(MAX_TABLE_ROWS + 7)]
        rendered = render_result(table(("a", "b"), rows, source=SOURCE))
        self.assertEqual(rendered.count("<tr>"), MAX_TABLE_ROWS + 1)
        self.assertIn("7 more row(s) not shown", rendered)

    def test_diff_lines_are_classified(self) -> None:
        rendered = diff_lines_html("--- a (now)\n+++ a (after)\n@@ -1,2 +1,2 @@\n context\n-old\n+new")
        self.assertIn('<span class="meta">--- a (now)</span>', rendered)
        self.assertIn('<span class="hunk">@@ -1,2 +1,2 @@</span>', rendered)
        self.assertIn('<span class="ctx"> context</span>', rendered)
        self.assertIn('<span class="del">-old</span>', rendered)
        self.assertIn('<span class="add">+new</span>', rendered)

    def test_line_charts_draw_a_polyline_and_bars_draw_rects(self) -> None:
        series = [{"name": "cpu", "values": [10, 20, 15]}]
        self.assertIn("<polyline", render_result(chart("line", series, source=SOURCE)))
        self.assertIn("<rect", render_result(chart("bar", series, source=SOURCE, units="%")))
        self.assertIn(">%<", render_result(chart("bar", series, source=SOURCE, units="%")))


class SourceAndOpenTests(unittest.TestCase):
    def test_a_web_source_links_out_and_a_file_source_opens_locally(self) -> None:
        web = render_result(text("x", source=SOURCE))
        self.assertIn('<a href="https://api.example.com/weather"', web)
        self.assertIn("capability: weather", web)
        local = render_result(text("x", source=FILE_SOURCE))
        report_url = open_url(REPORT_PATH)
        self.assertIn(f'<a href="{report_url}"', local)
        self.assertIn("iris://open?path=E%3A%5CDocs%5Creport.txt", local)

    def test_a_file_result_offers_open_and_folder(self) -> None:
        rendered = render_result(file(NOTES_PATH, source=SOURCE))
        file_url = open_url(NOTES_PATH)
        folder_url = open_url(DOCS_FOLDER)
        self.assertIn(f'<a href="{file_url}">Open</a>', rendered)
        self.assertIn(f'<a href="{folder_url}">Folder</a>', rendered)

    def test_a_local_image_is_served_from_disk_and_opens_on_click(self) -> None:
        rendered = render_result(image(mime_type="image/png", uri=SHOT_PATH, source=SOURCE))
        self.assertIn('src="file:///E:/Shots/cam%201.png"', rendered)
        image_url = open_url(SHOT_PATH).replace("&", "&amp;")
        self.assertIn(image_url, rendered)

    def test_every_result_shows_when_it_was_made(self) -> None:
        result = text("x", source=SOURCE)
        self.assertIn(f'<time datetime="{result.created_at}">', render_result(result))


class ConfirmationTests(unittest.TestCase):
    def test_a_reversible_change_shows_its_diff_and_the_undo_note(self) -> None:
        rendered = render_confirmation(
            {"title": "Update config", "summary": "Change web.timeout", "target": "config.json", "diff": "-a\n+b", "diff_path": "E:\\AI\\iris\\config.json"}
        )
        self.assertIn('<section class="confirmation">', rendered)
        self.assertIn("Update config", rendered)
        self.assertIn("Target: config.json", rendered)
        self.assertIn('<span class="add">+b</span>', rendered)
        self.assertIn("/undo can put it back", rendered)
        self.assertIn(f'href="{CONFIRM_URL}"', rendered)
        self.assertIn(f'href="{CANCEL_URL}"', rendered)

    def test_an_irreversible_action_is_marked_and_says_so(self) -> None:
        rendered = render_confirmation({"summary": "Replace the clipboard", "irreversible": True, "after": "hello"}, actions=False)
        self.assertIn('class="confirmation irreversible"', rendered)
        self.assertIn("This cannot be undone.", rendered)
        self.assertIn("<pre><code>hello</code></pre>", rendered)
        self.assertNotIn(CONFIRM_URL, rendered)

    def test_the_approval_bar_carries_both_choices(self) -> None:
        rendered = render_approval_bar()
        self.assertIn(CONFIRM_URL, rendered)
        self.assertIn(CANCEL_URL, rendered)


class PageTests(unittest.TestCase):
    def test_a_page_carries_its_theme_title_and_style(self) -> None:
        rendered = render_page(render_results([text("x", source=SOURCE)]), title="Weather", theme="dark")
        self.assertTrue(rendered.startswith('<!doctype html><html data-theme="dark">'))
        self.assertIn('<h2 class="panel-title">Weather</h2>', rendered)
        self.assertIn("--bg:", rendered)
        self.assertIn('html[data-theme="dark"]', rendered)

    def test_no_results_says_so(self) -> None:
        self.assertIn("No results.", render_results([]))


if __name__ == "__main__":
    unittest.main()
