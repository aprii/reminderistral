import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
templates = {
    p.name: p.read_text(encoding="utf-8")
    for p in sorted((root.parent / "templates").glob("*.html"))
}
css = (root.parent / "static" / "style.css").read_text(encoding="utf-8")

lines = ["TEMPLATES = {"]
for name, src in templates.items():
    lines.append(f"    {name!r}: {src!r},")
lines.append("}")
lines.append("")
lines.append(f"STATIC_CSS = {css!r}")
lines.append("")

out = root / "src" / "templates_data.py"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"wrote {out} with {len(templates)} templates + style.css")
