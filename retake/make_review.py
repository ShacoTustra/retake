"""Build a self-contained review page (review.html) next to the source video."""
import argparse
import json
import os
from pathlib import Path

TEMPLATE = Path(__file__).with_name("review_template.html")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("edits", type=Path, help="output of detect.py")
    ap.add_argument("-o", "--out", type=Path, default=None)
    args = ap.parse_args()

    d = json.loads(args.edits.read_text())
    src = Path(d["source"])
    out = args.out or src.with_name(src.stem + ".review.html")
    data = {
        "source": d["source"],
        "video": os.path.relpath(src, out.parent),
        "range": d["range"],
        "words": d["words"],
        "phrases": d["phrases"],
        "edits": d["edits"],
        "exportName": src.stem + ".edits.json",
    }
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", payload)
    out.write_text(html)
    print(out)


if __name__ == "__main__":
    main()
