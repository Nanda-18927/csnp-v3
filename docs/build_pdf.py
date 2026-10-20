"""Build a single printable HTML document from everything in docs/*.md.

Minimal, dependency-free Markdown -> HTML conversion: enough for the subset
these documents actually use (headings, tables, fenced code, lists, blockquotes,
rules, and inline bold/code). Output is written next to this script and then
rendered to PDF by Chrome headless:

    python docs/build_pdf.py
    chrome --headless --print-to-pdf docs/CRIME-SIGHT-NOIR.html docs/CRIME-SIGHT-NOIR.pdf
"""

import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# Document order, with the title each one gets in the combined file.
DOCS = [
    ("../README.md", "Overview"),
    ("ARCHITECTURE.md", "Architecture"),
    ("FRONTEND.md", "Frontend"),
    ("BACKEND.md", "Backend"),
    ("DATABASE.md", "Database"),
    ("API.md", "API Reference"),
    ("SECURITY.md", "Security"),
    ("DEPLOYMENT.md", "Deployment &amp; Operations"),
]


def inline(text):
    """Convert inline Markdown to HTML, escaping first so the source is safe."""
    t = html.escape(text, quote=False)
    # code spans first, so their contents are not treated as emphasis
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', t)
    return t


def convert(md):
    """Convert a Markdown document body to an HTML fragment."""
    out = []
    lines = md.split("\n")
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        # fenced code block
        if line.startswith("```"):
            lang = line[3:].strip()
            i += 1
            buf = []
            while i < n and not lines[i].startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = ' class="lang-%s"' % re.sub(r"\W", "", lang) if lang else ""
            out.append("<pre%s><code>%s</code></pre>" % (cls, html.escape("\n".join(buf))))
            continue

        # table: a pipe row followed by a separator row
        if line.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1]):
            def cells(row):
                return [c.strip() for c in row.strip().strip("|").split("|")]

            head = cells(line)
            i += 2
            body = []
            while i < n and lines[i].startswith("|"):
                body.append(cells(lines[i]))
                i += 1
            out.append("<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (
                "".join("<th>%s</th>" % inline(c) for c in head),
                "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % inline(c) for c in r) for r in body),
            ))
            continue

        # heading — the document's own H1 becomes the section title, so it is dropped
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            text = inline(m.group(2))
            if level == 1:
                pass  # the combined file supplies its own section heading
            else:
                out.append("<h%d>%s</h%d>" % (min(level, 5), text, min(level, 5)))
            i += 1
            continue

        if re.match(r"^-{3,}$", line.strip()):
            out.append("<hr>")
            i += 1
            continue

        # blockquote
        if line.startswith(">"):
            buf = []
            while i < n and lines[i].startswith(">"):
                buf.append(lines[i].lstrip(">").strip())
                i += 1
            out.append("<blockquote>%s</blockquote>" % inline(" ".join(buf)))
            continue

        # unordered list, allowing one level of nesting
        if re.match(r"^\s*[-*]\s+", line):
            buf = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                text = inline(re.sub(r"^\s*[-*]\s+", "", lines[i]))
                buf.append((indent, text))
                i += 1
            out.append(_list(buf))
            continue

        # ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            buf = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                indent = len(lines[i]) - len(lines[i].lstrip())
                text = inline(re.sub(r"^\s*\d+\.\s+", "", lines[i]))
                buf.append((indent, text))
                i += 1
            out.append(_olist(buf))
            continue

        if not line.strip():
            i += 1
            continue

        # paragraph — consume until a blank line or the start of another block
        buf = [line.strip()]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^(#{1,6}\s|```|\||>|[-*]\s|\d+\.\s|-{3,}$)", lines[i]
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append("<p>%s</p>" % inline(" ".join(buf)))

    return "\n".join(out)


def _list(items):
    """Render collected list items as a flat <ul>; indentation is cosmetic."""
    return "<ul>%s</ul>" % "".join("<li>%s</li>" % text for _, text in items)


def _olist(items):
    return "<ol>%s</ol>" % "".join("<li>%s</li>" % text for _, text in items)


CSS = """
@page { size: A4; margin: 17mm 15mm 18mm; }
* { box-sizing: border-box; }
body {
  font: 10.5pt/1.62 "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  color: #16202e; margin: 0; padding: 0;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
h1, h2, h3, h4, h5 { color: #0b2447; font-weight: 700; line-height: 1.25; page-break-after: avoid; }
h1 { font-size: 21pt; letter-spacing: .2pt; margin: 0 0 2mm; }
h2 { font-size: 14.5pt; margin: 11mm 0 3mm; padding-bottom: 1.5mm; border-bottom: 1.6pt solid #1c3a63; }
h3 { font-size: 11.8pt; margin: 7mm 0 2mm; color: #1c3a63; }
h4 { font-size: 10.6pt; margin: 5mm 0 1.5mm; color: #33507a; }
p { margin: 0 0 2.6mm; }
ul, ol { margin: 0 0 3mm; padding-left: 6mm; }
li { margin-bottom: 1.1mm; }
code {
  font-family: "Consolas", "Cascadia Mono", monospace; font-size: 8.9pt;
  background: #eef2f8; color: #0d3563; padding: .3mm 1.1mm; border-radius: 1.4pt;
}
pre {
  background: #0d1b30; color: #dce8f7; padding: 3mm 3.5mm; border-radius: 2pt;
  font-family: "Consolas", "Cascadia Mono", monospace; font-size: 8.1pt;
  line-height: 1.42; overflow-wrap: break-word; white-space: pre-wrap;
  page-break-inside: avoid; margin: 0 0 3.5mm;
}
pre code { background: none; color: inherit; padding: 0; font-size: inherit; }
table {
  width: 100%; border-collapse: collapse; margin: 0 0 4mm;
  font-size: 9.3pt; page-break-inside: avoid;
}
th {
  background: #1c3a63; color: #fff; text-align: left; font-weight: 600;
  padding: 1.6mm 2mm; border: .4pt solid #1c3a63;
}
td { padding: 1.5mm 2mm; border: .4pt solid #c3d0e2; vertical-align: top; }
tr:nth-child(even) td { background: #f4f7fb; }
blockquote {
  margin: 0 0 4mm; padding: 2.5mm 4mm; background: #eef4fb;
  border-left: 2.4pt solid #38a8e0; color: #2a3d55;
}
hr { border: 0; border-top: .6pt solid #c3d0e2; margin: 6mm 0; }
a { color: #1668a8; text-decoration: none; }
.cover { page-break-after: always; padding-top: 34mm; }
.cover .kicker {
  font-size: 9pt; letter-spacing: 3.4pt; text-transform: uppercase;
  color: #38a8e0; font-weight: 700; margin-bottom: 6mm;
}
.cover h1 { font-size: 34pt; letter-spacing: -.4pt; line-height: 1.1; margin-bottom: 5mm; }
.cover .sub { font-size: 13pt; color: #3b5573; max-width: 145mm; margin-bottom: 16mm; line-height: 1.55; }
.cover .rule { width: 46mm; height: 2.4pt; background: #38a8e0; margin-bottom: 14mm; }
.cover table { font-size: 10pt; width: 118mm; }
.cover .foot { margin-top: 20mm; font-size: 9.2pt; color: #6b7f98; border-top: .6pt solid #c3d0e2; padding-top: 4mm; }
.toc { page-break-after: always; }
.toc ol { padding-left: 7mm; }
.toc li { margin-bottom: 1.6mm; font-size: 10.6pt; }
.part { page-break-before: always; }
.part > h2 { border-bottom-width: 2.2pt; }
"""


def build():
    body = []

    cover = """
<div class="cover">
  <div class="kicker">Crime Sight Noir</div>
  <h1>CSNP v3<br>Technical Documentation</h1>
  <div class="rule"></div>
  <div class="sub">A 3D police-intelligence dashboard for peninsular South India.
  Flask and SQLite behind a single-page vanilla-JS frontend, where every screen
  &mdash; the command map, the case register, the criminal network, the analytics
  board &mdash; is rendered as WebGL geometry rather than a 2D chart.</div>
  <table>
    <tr><th>Layer</th><th>Technology</th></tr>
    <tr><td>Frontend</td><td>Vanilla JS &middot; three.js r147 &middot; no build step</td></tr>
    <tr><td>Backend</td><td>Python 3.12 &middot; Flask &middot; flask-cors</td></tr>
    <tr><td>Database</td><td>SQLite &middot; 6 tables &middot; no ORM</td></tr>
    <tr><td>Data</td><td>6,006 stations &middot; 212 criminals &middot; 191 officers &middot; 274 FIRs</td></tr>
    <tr><td>Tests</td><td>151 assertions, throwaway database</td></tr>
    <tr><td>Dependencies</td><td>2 third-party packages, both server-side</td></tr>
  </table>
  <div class="foot">Educational / coursework project. All station, officer and
  criminal records derive from the source workbooks in <code>data/</code>.</div>
</div>
"""

    toc = ['<div class="toc"><h2>Contents</h2><ol>']
    for _, title in DOCS:
        toc.append("<li>%s</li>" % title)
    toc.append("</ol></div>")

    body.append(cover)
    body.append("".join(toc))

    for rel, title in DOCS:
        path = os.path.normpath(os.path.join(HERE, rel))
        with open(path, encoding="utf-8") as fh:
            md = fh.read()
        body.append('<div class="part"><h2>%s</h2>%s</div>' % (title, convert(md)))

    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>Crime Sight Noir &mdash; CSNP v3 Technical Documentation</title>"
        "<style>%s</style></head><body>%s</body></html>"
        % (CSS, "\n".join(body))
    )

    out = os.path.join(HERE, "CRIME-SIGHT-NOIR.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)

    return out, len(page)


if __name__ == "__main__":
    path, size = build()
    print("wrote %s (%s KB)" % (path, round(size / 1024, 1)))