"""Bouwt twee Merchant Center-bestanden en een ChatGPT-productfeed voor Bushcraftshop.nl.

1. site/merchant-supplemental.xml  (aanvullende databron)
   Per product in de Google Shopping-feed (Lightspeed custom feed 30611): de volledige
   omschrijving, kleur, materiaal, gewicht, pluspunten en specificaties uit de producttekst.
   Producten met meerdere varianten krijgen ook item_group_id, maat en een titel met de maat.
   Dezelfde verrijking geldt voor de extra varianten uit bestand 2.

2. site/merchant-variants.xml  (extra primaire bron)
   Alle niet-standaardvarianten (andere maten/kleuren) van zichtbare producten met meerdere
   varianten. Lightspeed custom feeds geven alleen de standaardvariant door; deze bron vult
   de rest aan met eigen ID, prijs, EAN, voorraad en link (?id=<variant>). Categorie, merk,
   foto's en Ads-labels worden overgenomen van de standaardvariant in feed 30611.

3. site/openai-products.jsonl.gz  (productfeed voor ChatGPT, OpenAI-indeling)
   Alle varianten van de producten in feed 30611, één regel per variant, met de veldnamen
   uit de OpenAI Product Feed-specificatie (item_id, seller_name, target_countries enz.).
   Uitverkochte varianten van leverancier ENDOFLIFE of BCSOLDSTOCK blijven weg.

Nodig als GitHub Secrets: LIGHTSPEED_API_KEY en LIGHTSPEED_API_SECRET.
"""
import base64
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

BASE = "https://api.webshopapp.com/nl"
FEED_URL = "https://www.bushcraftshop.nl/feed/custom/30611/"
KEY = os.environ.get("LIGHTSPEED_API_KEY", "")
SECRET = os.environ.get("LIGHTSPEED_API_SECRET", "")
OUT_DIR = os.environ.get("OUT_DIR", "site")
G = "{http://base.google.com/ns/1.0}"
VALID_EAN = re.compile(r"^([0-9]{8}|[0-9]{12,14})$")


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


def fetch_feed():
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "Mozilla/5.0 bushcraftshop-feeds"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return ET.fromstring(r.read())


# ---------- tekstverrijking ----------

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


def enrichment(p):
    """Gedeelde verrijking voor alle varianten van een product (als XML-stukjes)."""
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

    parts = [f"<description>{escape(full_text(p))}</description>"]
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
    return "".join(parts)


# ---------- varianten ----------

def size_of(variant_title):
    t = (variant_title or "").strip()
    if t.lower().startswith("maat "):
        return t[5:].strip()
    return ""


def money(x):
    return f"{float(x):.2f} EUR"


def variant_item(v, default_item, product_id):
    """Volledig primair item voor een extra variant, gebaseerd op het feeditem van de standaardvariant."""
    base_title = default_item.find("title").text or ""
    link = default_item.find("link").text.split("?")[0]
    price, old = float(v.get("priceIncl") or 0), float(v.get("oldPriceIncl") or 0)
    tracking = v.get("stockTracking") == "enabled"
    in_stock = (v.get("stockLevel") or 0) > 0 if tracking else True
    parts = [
        f"<item><g:id>{v['id']}</g:id>",
        f"<title>{escape(base_title + ' - ' + (v.get('title') or '').strip())}</title>",
        f"<link>{escape(link)}?id={v['id']}</link>",
        f"<g:item_group_id>{product_id}</g:item_group_id>",
    ]
    sz = size_of(v.get("title"))
    if sz:
        parts.append(f"<g:size>{escape(sz)}</g:size>")
    parts.append(f"<g:availability>{'in_stock' if in_stock else 'out_of_stock'}</g:availability>")
    if old and old > price:
        parts.append(f"<g:price>{money(old)}</g:price><g:sale_price>{money(price)}</g:sale_price>")
    else:
        parts.append(f"<g:price>{money(price)}</g:price>")
    ean = (v.get("ean") or "").strip()
    code = (v.get("articleCode") or "").strip()
    if VALID_EAN.match(ean):
        parts.append(f"<g:gtin>{ean}</g:gtin>")
    if code:
        parts.append(f"<g:mpn>{escape(code)}</g:mpn>")
    if not VALID_EAN.match(ean) and not code:
        parts.append("<g:identifier_exists>no</g:identifier_exists>")
    # overnemen van de standaardvariant
    for tag in ["image_link", "additional_image_link", "condition", "brand", "product_type",
                "google_product_category", "custom_label_2", "custom_label_3", "custom_label_4"]:
        for el in default_item.findall(G + tag):
            parts.append(f"<g:{tag}>{escape(el.text or '')}</g:{tag}>")
    ship = "0.00 EUR" if price >= 99 else "6.99 EUR"
    parts.append(f"<g:shipping><g:country>NL</g:country><g:price>{ship}</g:price></g:shipping>")
    parts.append("</item>")
    return "".join(parts)


def write_rss(path, title, items):
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write('<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>\n')
        f.write(f"<title>{escape(title)}</title>\n<link>https://www.bushcraftshop.nl/</link>\n")
        f.write(f"<description>{escape(title)}</description>\n")
        f.write("\n".join(items))
        f.write("\n</channel></rss>\n")


def build(products, variants, feed_root):
    feed_items = {i.find(G + "id").text: i for i in feed_root.find("channel").findall("item")}
    by_product = {}
    for v in variants:
        by_product.setdefault(v["product"]["resource"]["id"], []).append(v)
    default_vid = {pid: next((v["id"] for v in vs if v.get("isDefault")), None) for pid, vs in by_product.items()}

    supplemental, extra = [], []
    stats = {"products": 0, "groups": 0, "extra_variants": 0, "extra_in_stock": 0}
    for p in products:
        if not p.get("isVisible"):
            continue
        vid = default_vid.get(p["id"])
        if vid is None or str(vid) not in feed_items:
            continue  # product staat niet in de Google-feed (bv. geen foto)
        stats["products"] += 1
        rich = enrichment(p)
        vs = by_product.get(p["id"], [])
        default_item = feed_items[str(vid)]
        default_v = next(v for v in vs if v["id"] == vid)
        if len(vs) > 1:
            stats["groups"] += 1
            sz = size_of(default_v.get("title"))
            title = (default_item.find("title").text or "") + " - " + (default_v.get("title") or "").strip()
            grp = f"<g:item_group_id>{p['id']}</g:item_group_id><title>{escape(title)}</title>"
            if sz:
                grp += f"<g:size>{escape(sz)}</g:size>"
            supplemental.append(f"<item><g:id>{vid}</g:id>{grp}{rich}</item>")
            for v in vs:
                if v["id"] == vid:
                    continue
                extra.append(variant_item(v, default_item, p["id"]))
                stats["extra_variants"] += 1
                if v.get("stockTracking") != "enabled" or (v.get("stockLevel") or 0) > 0:
                    stats["extra_in_stock"] += 1
                supplemental.append(f"<item><g:id>{v['id']}</g:id>{rich}</item>")
        else:
            supplemental.append(f"<item><g:id>{vid}</g:id>{rich}</item>")
    return supplemental, extra, stats


# ---------- ChatGPT-productfeed (OpenAI-indeling) ----------

SELLER_NAME = "Bushcraftshop"
SELLER_URL = "https://www.bushcraftshop.nl/"
RETURN_POLICY_URL = "https://www.bushcraftshop.nl/service/shipping-returns/"
TARGET_COUNTRIES = ["NL", "BE"]
STORE_COUNTRY = "NL"
CLEARANCE_SUPPLIERS = {"ENDOFLIFE", "BCSOLDSTOCK"}


def gtin_ok(g):
    """8, 12, 13 of 14 cijfers met een kloppend controlecijfer."""
    if not re.fullmatch(r"[0-9]{8}|[0-9]{12,14}", g or ""):
        return False
    digits = [int(c) for c in g]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits[:-1])))
    return (10 - total % 10) % 10 == digits[-1]


def weight_parts(v):
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|gram|g)\b", v or "", re.I)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    if n <= 0:
        return None
    return f"{n:g}", ("kg" if m.group(2).lower() == "kg" else "g")


def supplier_id(p):
    s = p.get("supplier")
    if isinstance(s, dict):
        return (s.get("resource") or {}).get("id")
    return None


def openai_rows(products, variants, feed_root, clearance_ids):
    feed_items = {i.find(G + "id").text: i for i in feed_root.find("channel").findall("item")}
    by_product = {}
    for v in variants:
        by_product.setdefault(v["product"]["resource"]["id"], []).append(v)

    rows = []
    stats = {"rows": 0, "in_stock": 0, "groups": 0, "skipped_no_brand": 0,
             "skipped_clearance_sold_out": 0, "skipped_no_price": 0}
    for p in products:
        if not p.get("isVisible"):
            continue
        vs = by_product.get(p["id"], [])
        vid = next((v["id"] for v in vs if v.get("isDefault")), None)
        if vid is None or str(vid) not in feed_items:
            continue
        item = feed_items[str(vid)]
        brand = (item.findtext(G + "brand") or "").strip()
        if not brand:
            stats["skipped_no_brand"] += len(vs)
            continue
        base_title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").split("?")[0]
        image = (item.findtext(G + "image_link") or "").strip()
        if not base_title or not link or not image:
            continue
        extra_images = [e.text.strip() for e in item.findall(G + "additional_image_link") if e.text]
        category = (item.findtext(G + "product_type") or "").strip()
        description = full_text(p) or (item.findtext("description") or "").strip()
        if not description:
            continue

        h = p.get("content") or ""
        spec = {}
        for li in list_items(section(h, "Specificaties")):
            if ":" in li:
                k, val = li.split(":", 1)
                spec[k.strip().lower()] = val.strip()
        color = clean_color(spec.get("kleur", ""))
        material = spec.get("samenstelling") or spec.get("materiaal", "")
        wp = weight_parts(spec.get("gewicht", ""))
        clearance = supplier_id(p) in clearance_ids
        multi = len(vs) > 1
        emitted = 0

        for v in vs:
            price, old = float(v.get("priceIncl") or 0), float(v.get("oldPriceIncl") or 0)
            if price <= 0:
                stats["skipped_no_price"] += 1
                continue
            in_stock = (v.get("stockLevel") or 0) > 0 if v.get("stockTracking") == "enabled" else True
            if clearance and not in_stock:
                stats["skipped_clearance_sold_out"] += 1
                continue
            vtitle = (v.get("title") or "").strip()
            row = {
                "item_id": str(v["id"]),
                "title": (f"{base_title} - {vtitle}" if multi and vtitle else base_title)[:150],
                "description": description,
                "url": link if v["id"] == vid and not multi else f"{link}?id={v['id']}",
                "brand": brand,
                "seller_name": SELLER_NAME,
                "seller_url": SELLER_URL,
                "image_url": image,
                "availability": "in_stock" if in_stock else "out_of_stock",
                "price": money(old) if old > price else money(price),
                "target_countries": TARGET_COUNTRIES,
                "store_country": STORE_COUNTRY,
                "condition": "new",
                "is_eligible_search": True,
                "accepts_returns": True,
                "return_deadline_in_days": 14,
                "return_policy": RETURN_POLICY_URL,
            }
            if old > price:
                row["sale_price"] = money(price)
            if extra_images:
                row["additional_image_urls"] = extra_images
            if multi:
                row["group_id"] = f"P{p['id']}"
                row["listing_has_variations"] = True
                sz = size_of(vtitle)
                if sz:
                    row["variant_dict"] = {"Maat": sz}
                    row["size"] = sz
                elif vtitle:
                    row["variant_dict"] = {"Uitvoering": vtitle}
            ean = (v.get("ean") or "").strip()
            if gtin_ok(ean):
                row["gtin"] = ean
            code = (v.get("articleCode") or "").strip()
            if code:
                row["mpn"] = code
            if category:
                row["product_category"] = category
            if color:
                row["color"] = color
            if material and len(material) <= 200:
                row["material"] = material
            if wp:
                row["weight"], row["item_weight_unit"] = wp
            rows.append(row)
            emitted += 1
            stats["rows"] += 1
            stats["in_stock"] += in_stock
        if multi and emitted:
            stats["groups"] += 1
    return rows, stats


def write_openai(path, rows):
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def main():
    if not KEY or not SECRET:
        sys.exit("LIGHTSPEED_API_KEY en LIGHTSPEED_API_SECRET ontbreken (GitHub Secrets).")
    products = fetch_all("/products.json", "products", "id,isVisible,description,content,supplier")
    variants = fetch_all("/variants.json", "variants",
                         "id,isDefault,product,title,stockLevel,stockTracking,priceIncl,oldPriceIncl,ean,articleCode")
    feed_root = fetch_feed()
    supplemental, extra, stats = build(products, variants, feed_root)
    if stats["products"] < 500:
        sys.exit(f"Slechts {stats['products']} producten gevonden; bestanden niet bijgewerkt.")

    suppliers = fetch_all("/suppliers.json", "suppliers", "id,title")
    clearance_ids = {x["id"] for x in suppliers if (x.get("title") or "").strip().upper() in CLEARANCE_SUPPLIERS}
    rows, ostats = openai_rows(products, variants, feed_root, clearance_ids)
    if ostats["rows"] < 500:
        sys.exit(f"Slechts {ostats['rows']} regels voor de ChatGPT-feed; bestanden niet bijgewerkt.")

    os.makedirs(OUT_DIR, exist_ok=True)
    write_openai(os.path.join(OUT_DIR, "openai-products.jsonl.gz"), rows)
    write_rss(os.path.join(OUT_DIR, "merchant-supplemental.xml"),
              "Bushcraftshop.nl - aanvullende productdata", supplemental)
    write_rss(os.path.join(OUT_DIR, "merchant-variants.xml"),
              "Bushcraftshop.nl - extra varianten (maten/kleuren)", extra)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write("<!doctype html><meta charset='utf-8'><title>Bushcraftshop feeds</title>"
                "<p><a href='merchant-supplemental.xml'>merchant-supplemental.xml</a></p>"
                "<p><a href='merchant-variants.xml'>merchant-variants.xml</a></p>"
                "<p><a href='openai-products.jsonl.gz'>openai-products.jsonl.gz</a></p>")
    print(json.dumps(stats))
    print(json.dumps(ostats))


if __name__ == "__main__":
    main()
