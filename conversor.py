#!/usr/bin/env python3
import os, argparse, tempfile
from pathlib import Path
from collections import Counter
from ebooklib import epub
import fitz  # PyMuPDF

COVER_MAX_W = 480
COVER_MAX_H = 800
COLUMN_SPLIT = 0.5  # page width fraction dividing left/right columns
HEADING_RATIO = 1.15  # font size must exceed body_size * this to be a heading


# Cover


def resolve_cover(cover_arg, pdf_path):
    if cover_arg and os.path.isfile(cover_arg):
        return cover_arg
    for candidate in [
        Path(pdf_path).parent / "cover.jpg",
        Path(__file__).parent / "cover.jpg",
    ]:
        if candidate.is_file():
            return str(candidate)
    return None


def prepare_cover(src, tmp_dir):
    from PIL import Image

    img = Image.open(src).convert("L")
    img.thumbnail((COVER_MAX_W, COVER_MAX_H), Image.LANCZOS)
    out = os.path.join(tmp_dir, "cover.jpg")
    img.save(out, "JPEG", quality=85, optimize=True)
    return out


# PDF metadata


def read_metadata(pdf_path):
    doc = fitz.open(pdf_path)
    m = doc.metadata or {}
    return {
        "title": m.get("title", "").strip(),
        "author": m.get("author", "").strip(),
        "pages": len(doc),
    }


# Layout detection


def is_two_column(doc, sample=3):
    # Count text blocks whose left edge sits in the right half of the page.
    # A true 2-col layout has many such blocks; single-col docs have few.
    right_count = 0
    total = 0
    for i in range(min(sample, len(doc))):
        page = doc[i]
        mid = page.rect.width * COLUMN_SPLIT
        for b in page.get_text("blocks"):
            if b[6] != 0:  # skip image blocks
                continue
            total += 1
            if b[0] > mid:  # b[0] is x0 of the block
                right_count += 1
    return total > 0 and (right_count / total) > 0.25


def body_font_size(doc, sample=5):
    # Weighted by character count — the most common size is the body size.
    sizes = Counter()
    for i in range(min(sample, len(doc))):
        for block in doc[i].get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    t = span["text"].strip()
                    if t:
                        sizes[round(span["size"], 1)] += len(t)
    return sizes.most_common(1)[0][0] if sizes else 10.0


#  Block extraction


def blocks_from_page(page, body_size, two_col):
    # Each PyMuPDF block already groups lines that belong together.
    # We classify the whole block as heading or paragraph by its dominant font size.
    raw = page.get_text("dict")["blocks"]
    blocks = []
    for b in raw:
        if b.get("type") != 0:
            continue
        # Collect all spans in this block
        spans = [s for line in b["lines"] for s in line["spans"]]
        if not spans:
            continue
        # Dominant size = size of the span with most characters
        dominant_size = max(spans, key=lambda s: len(s["text"]))["size"]
        text = " ".join(s["text"].strip() for s in spans if s["text"].strip())
        if not text:
            continue
        is_heading = dominant_size >= body_size * HEADING_RATIO
        blocks.append(
            {
                "lines": b["lines"],
                "heading": is_heading,
                "x0": b["bbox"][0],
                "y0": b["bbox"][1],
            }
        )

    if two_col:
        mid = page.rect.width * COLUMN_SPLIT
        left = [b for b in blocks if b["x0"] < mid]
        right = [b for b in blocks if b["x0"] >= mid]
        # Sort each column top-to-bottom, then concatenate left+right
        blocks = sorted(left, key=lambda b: b["y0"]) + sorted(
            right, key=lambda b: b["y0"]
        )
    else:
        blocks = sorted(blocks, key=lambda b: b["y0"])

    return blocks


#  Page-per-chapter grouping
# CrossPoint has no "go to page" — only "go to %".
# Each PDF page becomes one EPUB spine item so the TOC is navigable by page.


def group_chapters(pages_blocks, meta):
    chapters = []
    for i, blocks in enumerate(pages_blocks):
        if not blocks:
            continue
        chapters.append(
            {
                "title": f"p. {i + 1}",
                "blocks": blocks,
            }
        )
    return chapters or [{"title": "p. 1", "blocks": []}]


#  EPUB assembly

# CrossPoint runs on ESP32 (~380 KB RAM) - keep CSS minimal
EPUB_CSS = (
    "body { font-family: serif; font-size: 1em; line-height: 1.5; margin: 0.8em; }\n"
    "h1   { font-size: 1.4em; font-weight: bold; margin: 1.2em 0 0.4em 0; }\n"
    "h2   { font-size: 1.1em; font-weight: bold; margin: 1em 0 0.3em 0; }\n"
    "p    { margin: 0.3em 0; text-indent: 1em; }\n"
)


def span_to_html(span, base_size, base_y):
    import html as hl

    text = hl.escape(span["text"])

    size = span.get("size", base_size)
    bbox = span.get("bbox", [0, 0, 0, 0])
    y = bbox[1]

    flags = span.get("flags", 0)
    is_italic = flags & 2
    is_bold = flags & 1

    is_sup = False
    is_sub = False

    # via rise
    rise = span.get("rise", 0)
    if rise > 1:
        is_sup = True
    elif rise < -1:
        is_sub = True

    # fallback
    else:
        if size < base_size * 0.8:
            if y < base_y - 1:
                is_sup = True
            elif y > base_y + 1:
                is_sub = True

    # tags
    if is_sup:
        text = f"<sup>{text}</sup>"
    elif is_sub:
        text = f"<sub>{text}</sub>"

    if is_italic:
        text = f"<i>{text}</i>"
    if is_bold:
        text = f"<b>{text}</b>"

    return text


def blocks_to_html(blocks):
    parts = []

    for b in blocks:
        html_spans = []

        for line in b.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            base_span = max(spans, key=lambda s: s["size"])
            base_size = base_span["size"]
            base_y = base_span["bbox"][1]

            for span in spans:
                if span["text"].strip():
                    html_spans.append(span_to_html(span, base_size, base_y))

        text = " ".join(html_spans)
        tag = "h2" if b["heading"] else "p"
        parts.append(f"<{tag}>{text}</{tag}>")

    return "\n".join(parts)


def make_xhtml(title, body_html):
    import html as hl

    t = hl.escape(title)
    return (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="pt">\n'
        f"<head><title>{t}</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        f"<body>\n{body_html}\n</body></html>"
    ).encode("utf-8")


def build_epub(meta, chapters, cover_path, out_path):
    book = epub.EpubBook()
    book.set_identifier(f"pdf2epub-{abs(hash(out_path)):08x}")
    book.set_title(meta["title"] or Path(out_path).stem)
    book.set_language("pt")
    if meta["author"]:
        book.add_author(meta["author"])

    cover_item = None
    if cover_path:
        with open(cover_path, "rb") as f:
            cover_data = f.read()
        book.set_cover("cover.jpg", cover_data, create_page=False)
        cover_item = epub.EpubHtml(
            title=meta["title"], file_name="cover.xhtml", lang="pt"
        )
        cover_item.set_content(
            f"""<?xml version='1.0' encoding='utf-8'?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
<title>{meta["title"]}</title>
</head>
<body style="margin:0;padding:0;text-align:center;">

<img src="cover.jpg" alt="cover" style="width:100%;height:auto;background:transparent;"/>

</body></html>""".encode(
                "utf-8"
            )
        )
        book.add_item(cover_item)

    css = epub.EpubItem(
        uid="css", file_name="style.css", media_type="text/css", content=EPUB_CSS
    )
    book.add_item(css)

    epub_chapters = []
    for i, chap in enumerate(chapters):
        c = epub.EpubHtml(title=chap["title"], file_name=f"c{i+1:03d}.xhtml", lang="pt")
        c.set_content(make_xhtml(chap["title"], blocks_to_html(chap["blocks"])))
        c.add_item(css)
        book.add_item(c)
        epub_chapters.append(c)

    toc_items = []

    if cover_item:
        toc_items.append(epub.Link("cover.xhtml", meta["title"], "cover"))

    for i, c in enumerate(epub_chapters):
        toc_items.append(epub.Link(c.file_name, c.title, f"n{i}"))

    book.toc = tuple(toc_items)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    # Cover first in spine so the reader opens on it
    # "nav" is required by the EPUB spec but excluded from spine so it never renders as a page
    spine = ([cover_item] if cover_item else []) + epub_chapters
    book.spine = spine
    epub.write_epub(out_path, book)


#  Entry point


def convert(pdf_path, output_dir, title, author, cover_arg):
    print(os.path.basename(pdf_path))

    meta = read_metadata(pdf_path)
    if title:
        meta["title"] = title
    if author:
        meta["author"] = author
    if not meta["title"]:
        meta["title"] = Path(pdf_path).stem.replace("_", " ").replace("-", " ").title()

    doc = fitz.open(pdf_path)
    two_col = is_two_column(doc)
    bsize = body_font_size(doc)

    print(f"  pages={meta['pages']}  two_col={two_col}  body_size={bsize}")
    print(f"  title:  {meta['title']}")
    print(f"  author: {meta['author'] or '-'}")

    pages_blocks = [blocks_from_page(page, bsize, two_col) for page in doc]
    chapters = group_chapters(pages_blocks, meta)
    print(f"  chapters: {len(chapters)}")

    out_file = os.path.join(output_dir, Path(pdf_path).stem + ".epub")

    with tempfile.TemporaryDirectory() as tmp:
        cover_src = resolve_cover(cover_arg, pdf_path)
        cover_ready = prepare_cover(cover_src, tmp) if cover_src else None
        if cover_ready:
            print(f"  cover: {os.path.basename(cover_src)}")
        build_epub(meta, chapters, cover_ready, out_file)

    print(f"  -> {out_file} ({os.path.getsize(out_file) // 1024} KB)")
    return out_file


def main():
    p = argparse.ArgumentParser(description="PDF to EPUB for CrossPoint 1.1.1")
    p.add_argument("pdfs", nargs="+")
    p.add_argument("-o", "--output", default=".")
    p.add_argument("--cover", default="", help="Cover image (jpg/png)")
    p.add_argument("--title", default="")
    p.add_argument("--author", default="")
    args = p.parse_args()

    os.makedirs(args.output, exist_ok=True)
    ok, fail = [], []
    for pdf in args.pdfs:
        if not os.path.isfile(pdf):
            print(f"not found: {pdf}")
            fail.append(pdf)
            continue
        try:
            ok.append(convert(pdf, args.output, args.title, args.author, args.cover))
        except Exception as e:
            import traceback

            traceback.print_exc()
            fail.append(pdf)

    if len(args.pdfs) > 1:
        print(f"\nok={len(ok)} fail={len(fail)}")


if __name__ == "__main__":
    main()
