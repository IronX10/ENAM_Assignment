"""Bundle app/ into one page (inline CSS and JS) for hosting as a single-file demo.
Data stays in data/*.json next to it."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
css = open(os.path.join(ROOT, "app", "styles.css"), encoding="utf-8").read()
js = open(os.path.join(ROOT, "app", "app.js"), encoding="utf-8").read()
page = f"""<title>Delta</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>
{css}
</style>
<div class="app"><main id="main" aria-live="polite"></main></div>
<div id="sheet"></div>
<div class="tabs" id="tabs"></div>
<script>
{js}
</script>
"""
os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
open(os.path.join(ROOT, "dist", "delta.html"), "w", encoding="utf-8").write(page)
print("dist/delta.html", len(page))
