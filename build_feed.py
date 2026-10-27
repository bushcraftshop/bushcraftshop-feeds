"""Bouwt de aanvullende Merchant Center-databron voor Bushcraftshop.nl.

Haalt alle zichtbare producten uit de Lightspeed eCom API (NL), leest uit de
producttekst (standaardformat) de volledige omschrijving, kleur, materiaal,
gewicht, pluspunten en specificaties, en schrijft site/merchant-supplemental.xml.

Het g:id is de standaardvariant van het product, gelijk aan product.vid in de
Google Shopping-feed (Lightspeed custom feed 30611).

Nodig als GitHub Secrets: LIGHTSPEED_API_KEY en LIGHTSPEED_API_SECRET.
"""
import base64
import html
import json
import os
import re
import sys
import time
import urllib.request
from xml.sax.saxutils import escape

BASE = "https://api.webshopapp.com/nl"
KEY = os.environ.get("LIGHTSPEED_API_KEY", "")
SECRET = os.environ.get("LIGHTSPEED_API_SECRET", "")
OUT = os.environ.get("OUT_FILE", "site/merchant-supplemental.xml")


def ls_get(path, params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{BASE}{path}?{query}")
    token = base64.b64encode(f"{KEY}:{SECRET}".encode()).decode()
    req.add_header("Authorization", f"Basic {token}")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(30)
                continue
            raise


def fetch_all(path, key, fields):
    out, page = [], 1
    while True:
        items = ls_get(path, {"limit": 250, "page": page, "fields": fields}).get(key, [])
        out.extend(items)
        if len(items) < 250:
            return out
        page += 1


def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def section(h, name):
    m = re.search(r"<h[23][^>]*>\s*" + name + r".*?</h[23]>(.*?)(?=<h[23]|$)", h or "", re.S | re.I)
    return m.group(1) if m else ""


def list_items(h):
    return [clean(x) for x in re.findall(r"<li[^>]*>(.*?)</li>", h or "", re.S)]


def full_text(p):
    h = p.get("content") or ""
    h = re.sub(r"<(h2|h3)[^>]*>(.*?)</\1>", lambda m: "\n\n" + clean(m.group(2)) + "\n", h, flags=re.S)
    h = re.sub(r"<li[^>]*>(.*?)</li>", lambda m: "\n- " + clean(m.group(1)), h, flags=re.S)
    h = re.sub(r"</p>", "\n\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    d = (p.get("description") or "").strip()
    out = f"{d}\n\n{t}".strip() if d else t
    if len(out) > 5000:
        out = out[:4990].rsplit(" ", 1)[0] + " …"
    return out


def weight(v):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|gram|g)\b", v or "", re.I)
    if not m:
        return ""
    n = float(m.group(1).replace(",", "."))
    return f"{n:g} kg" if m.group(2).lower() == "kg" else f"{n:g} g"


def clean_color(c):
    c = re.sub(r"\s*\(.*?\)", "", c or "").strip()
    if len(c) > 40:
        c = re.split(r",| met | en ", c)[0].strip()
    if len(c) > 40 or c.lower().startswith("ongekleurd"):
        return ""
    return c[:1].upper() + c[1:]


def build_item(vid, p):
    h = p.get("content") or ""
    spec = []
    for li in list_items(section(h, "Specificaties")):
        if ":" in li:
            k, v = li.split(":", 1)
            spec.append((k.strip(), v.strip()))
    s = {k.lower(): v for k, v in spec}
    pros = [re.sub(r"^✅\s*", "", x) for x in list_items(section(h, "Plus")) if x.startswith("✅")]
    color = clean_color(s.get("kleur", ""))
    material = s.get("samenstelling") or s.get("materiaal", "")
    w = weight(s.get("gewicht", ""))

    parts = [f"<item><g:id>{vid}</g:id>", f"<description>{escape(full_text(p))}</description>"]
    if color:
        parts.append(f"<g:color>{escape(color)}</g:color>")
    if material and len(material) <= 200:
        parts.append(f"<g:material>{escape(material)}</g:material>")
    if w:
        parts.append(f"<g:product_weight>{escape(w)}</g:product_weight>")
    for x in pros[:10]:
        parts.append(f"<g:product_highlight>{escape(x[:150])}</g:product_highlight>")
    for k, v in [(k, v) for k, v in spec if k.lower() != "merk" and v][:30]:
        parts.append(
            "<g:product_detail><g:section_name>Specificaties</g:section_name>"
            f"<g:attribute_name>{escape(k[:140])}</g:attribute_name>"
            f"<g:attribute_value>{escape(v[:1000])}</g:attribute_value></g:product_detail>"
        )
    parts.append("</item>")
    return "".join(parts)


def main():
    if not KEY or not SECRET:
        sys.exit("LIGHTSPEED_API_KEY en LIGHTSPEED_API_SECRET ontbreken (GitHub Secrets).")
    products = fetch_all("/products.json", "products", "id,isVisible,description,content")
    variants = fetch_all("/variants.json", "variants", "id,isDefault,product")
    default_vid = {v["product"]["resource"]["id"]: v["id"] for v in variants if v.get("isDefault")}

    items = [build_item(default_vid[p["id"]], p) for p in products if p.get("isVisible") and p["id"] in default_vid]
    if len(items) < 500:
        sys.exit(f"Slechts {len(items)} producten gevonden; bestand niet bijgewerkt.")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>\n')
        f.write("<title>Bushcraftshop.nl - aanvullende productdata</title>\n")
        f.write("<link>https://www.bushcraftshop.nl/</link>\n")
        f.write("<description>Omschrijving, kleur, materiaal, gewicht, highlights en specificaties</description>\n")
        f.write("\n".join(items))
        f.write("\n</channel></rss>\n")
    with open(os.path.join(os.path.dirname(OUT), "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'><title>Bushcraftshop feeds</title>"
                "<p><a href='merchant-supplemental.xml'>merchant-supplemental.xml</a></p>")
    print(f"{len(items)} producten geschreven naar {OUT}")


if __name__ == "__main__":
    main()
