"""
Build a realistic *labeled* corpus for evaluating the Tier-1 tag adapter.

Source: the OpenAlex API (free, no auth) - real scholarly abstracts with real
multi-label keyword/concept tags. We seed a controlled vocabulary of mid-level
concepts spanning several fields, INCLUDING deliberately confusable near-siblings
(deep learning / machine learning / computer vision; genetics / genomics), since
that near-synonym crowding is exactly where a tag-supervised projection should
earn its keep. Each work is tagged with whichever seed concepts it carries, so
multi-label overlap arises naturally and every seed tag has solid support.

Writes corpus.json: [{"id","title","text","tags":[...]}, ...] - cached so the
eval harness re-runs without re-pulling. Re-run this only to refresh the corpus.

Run:  python build_eval_corpus.py [works_per_seed]
"""
import json, sys, ssl, time, urllib.request, urllib.parse
from collections import Counter

MAILTO = "timcorr91@gmail.com"
CTX = ssl.create_default_context()

# Mid-level seed concepts; near-sibling clusters are intentional.
SEEDS = [
    # ML/AI cluster (confusable on purpose)
    "Deep learning", "Machine learning", "Computer vision",
    "Natural language processing", "Reinforcement learning",
    # security / systems
    "Cryptography", "Computer security",
    # bio cluster (confusable)
    "Genetics", "Genomics", "Immunology", "Neuroscience",
    # physical sciences
    "Astrophysics", "Condensed matter physics", "Climatology",
    # other domains for cross-field contrast
    "Epidemiology", "Macroeconomics", "Materials science", "Robotics",
    # aerospace/aircraft cluster (domain-level; OpenAlex has no F-15E-vs-F-15EX
    # variant granularity - that low-support case is the code-boost layer's job)
    "Aerospace engineering", "Aerodynamics", "Avionics", "Jet engine",
]

# HARD vocabulary: fine-grained tags that all sit INSIDE a shared parent topic,
# so a generic encoder sees them as "the same kind of text" and discrimination
# is weak - the regime where a tag-supervised projection should actually earn
# its keep. Three near-synonym families: NLP tasks, CV tasks, DL architectures.
SEEDS_HARD = [
    # NLP tasks (all "natural language processing")
    "Named-entity recognition", "Machine translation", "Question answering",
    "Sentiment analysis", "Automatic summarization", "Coreference",
    # computer-vision tasks (all "computer vision")
    "Object detection", "Image segmentation", "Pose", "Facial recognition system",
    "Optical character recognition", "Image retrieval",
    # deep-learning architectures (all "deep learning")
    "Convolutional neural network", "Recurrent neural network",
    "Generative adversarial network", "Transformer", "Autoencoder",
    "Graph (abstract data type)",
]


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "clipper-eval/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=30, context=CTX).read())


def resolve_concept(name):
    q = urllib.parse.quote(name)
    d = _get(f"https://api.openalex.org/concepts?search={q}&per-page=1&mailto={MAILTO}")
    if not d.get("results"):
        return None
    r = d["results"][0]
    return r["id"].rsplit("/", 1)[-1], r["display_name"]  # bare Cxxxx, canonical name


def reconstruct_abstract(inv):
    if not inv:
        return ""
    pos = [(p, w) for w, ps in inv.items() for p in ps]
    pos.sort()
    return " ".join(w for _, w in pos)


def fetch_works(cid, per_seed):
    sel = "id,title,abstract_inverted_index,concepts,keywords"
    url = (f"https://api.openalex.org/works?filter=concepts.id:{cid},"
           f"has_abstract:true,language:en&per-page={per_seed}"
           f"&select={sel}&mailto={MAILTO}")
    return _get(url).get("results", [])


def main():
    args = [a for a in sys.argv[1:]]
    hard = "hard" in args
    nums = [int(a) for a in args if a.isdigit()]
    per_seed = nums[0] if nums else 22
    seeds = SEEDS_HARD if hard else SEEDS
    outfile = "corpus_hard.json" if hard else "corpus.json"
    print(f"mode: {'HARD (near-synonym intra-field)' if hard else 'broad fields'} "
          f"-> {outfile}\n")
    # Resolve seed names -> canonical concept ids; build a set so we can tag
    # each work with the seeds it actually carries.
    seed_ids, seed_name = {}, {}
    for nm in seeds:
        try:
            cid, canon = resolve_concept(nm)
            seed_ids[cid] = canon
            seed_name[nm] = (cid, canon)
            print(f"  seed {nm:28} -> {cid} ({canon})")
        except Exception as e:
            print(f"  seed {nm:28} ERR {e!r}")
        time.sleep(0.2)

    works = {}  # id -> record (dedup across seeds)
    for nm, (cid, canon) in seed_name.items():
        try:
            rs = fetch_works(cid, per_seed)
        except Exception as e:
            print(f"  fetch {nm} ERR {e!r}")
            continue
        for w in rs:
            wid = w["id"].rsplit("/", 1)[-1]
            text = reconstruct_abstract(w.get("abstract_inverted_index"))
            if len(text) < 250:           # too short to be a fair document
                continue
            # tags = the SEED concepts this work carries (controlled vocab,
            # guaranteed support), by concept id intersection.
            wcids = {c["id"].rsplit("/", 1)[-1] for c in w.get("concepts", [])}
            tags = sorted({seed_ids[c] for c in wcids if c in seed_ids})
            if len(tags) < 1:
                tags = [canon]            # at least the seed it was fetched under
            works[wid] = {"id": wid, "title": (w.get("title") or "")[:300],
                          "text": text, "tags": tags}
        print(f"  {nm:28} +{len(rs)} works (corpus now {len(works)})")
        time.sleep(0.3)

    corpus = list(works.values())
    json.dump(corpus, open(outfile, "w", encoding="utf-8"), ensure_ascii=False)
    # stats
    supp = Counter(t for c in corpus for t in c["tags"])
    ml = sum(1 for c in corpus if len(c["tags"]) >= 2)
    print(f"\n  corpus: {len(corpus)} clips, {len(supp)} tags")
    print(f"  multi-label clips (>=2 tags): {ml}")
    print(f"  tags with >=5 / >=10 support: "
          f"{sum(1 for v in supp.values() if v>=5)} / {sum(1 for v in supp.values() if v>=10)}")
    print("  support:", dict(supp.most_common()))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
