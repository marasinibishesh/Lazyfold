"""Write packaging/third_party/THIRD-PARTY-NOTICES[-FULL].txt from the build
environment's installed package metadata, so the list matches what is bundled.

Run with the build environment's python:
  .venv-build\\Scripts\\python packaging\\make_notices.py lean
  .venv-build-full\\Scripts\\python packaging\\make_notices.py full
"""

import sys
from importlib.metadata import distributions
from pathlib import Path

OUT = Path(__file__).resolve().parent / "third_party"
SKIP = {"pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile",
        "pywin32-ctypes", "packaging"}  # build tools, not shipped inside the app


def license_of(dist) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return "; ".join(dict.fromkeys(classifiers))
    lic = (md.get("License") or "").strip().splitlines()
    return lic[0][:80] if lic and lic[0] else "see the project's license file"


def main(edition: str) -> None:
    rows = []
    for d in distributions():
        name = d.metadata.get("Name") or ""
        if not name or name.lower() in SKIP:
            continue
        rows.append((name, d.version, license_of(d), d.metadata.get("Home-page") or ""))
    rows.sort(key=lambda r: r[0].lower())
    full = edition == "full"
    head = [f"SlothFile{' (Full edition)' if full else ''} - third-party notices", "=" * 48, "",
            "SlothFile is (c) 2026 Bishesh Marasini and released under the MIT License.", ""]
    if full:
        head += ["Built with PriorLabs-TabPFN.", "",
                 "This edition includes the TabPFN v2 classifier model weights",
                 "(tabpfn-v2-classifier-finetuned-zk73skhh.ckpt) by Prior Labs GmbH, redistributed",
                 "unmodified under the Prior Labs License version 1.1 (Apache 2.0 with an added",
                 "attribution clause). A copy is installed next to this file as TabPFN-v2-LICENSE.txt.",
                 "Source of the weights: https://huggingface.co/Prior-Labs/TabPFN-v2-clf",
                 "The tabpfn Python package itself is Apache-2.0 (https://github.com/PriorLabs/TabPFN).",
                 "SlothFile uses TabPFN only on this computer to estimate whether you would accept a",
                 "proposed move. It never trains or distributes a model.", ""]
    head += ["The app also bundles the Python runtime (PSF License) and these packages. Their",
             "license texts are inside the app's _internal folder (in each *.dist-info folder) where",
             "the package ships one:", ""]
    body = [f"  {n} {v} - {lic}" for n, v, lic, _ in rows]
    name = "THIRD-PARTY-NOTICES-FULL.txt" if full else "THIRD-PARTY-NOTICES.txt"
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text("\n".join(head + body) + "\n", encoding="utf-8")
    print(f"wrote {OUT / name} ({len(rows)} packages)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "lean")
