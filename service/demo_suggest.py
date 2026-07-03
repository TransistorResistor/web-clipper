"""Manual demo of tag suggestion over a few sample articles.

Seeds a small, realistic library across several topics, then runs
suggest_tags() on sample documents and prints the ranked chips with the
signal that produced each one. Lets you eyeball that:
  - existing tags come back for on-topic articles (similar / predicted),
  - co-occurrence kicks in once you've chosen a tag,
  - a novel article gets brand-new keyphrase tags (＋), and
  - unrelated tags stay out.

Run: python demo_suggest.py
"""
import os, sys, tempfile

try:                       # console may be cp1252 on Windows; the marks are UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ["CLIPPER_DB"] = tempfile.mktemp(suffix=".db")
from db import connect, init_db          # noqa: E402
init_db()
import app                               # noqa: E402
import suggest as SG                     # noqa: E402

c = app.app.test_client()

WHY = {"similar": "≈ similar", "predicted": "◎ predicted",
       "co-occurring": "↔ co-occurs",
       "both": "★ multiple", "keyword": "＋ NEW"}


def add(title, body, tags, url=""):
    r = c.post("/api/clip", json={"title": title, "html": f"<p>{body}</p>",
                                  "url": url, "tags": tags}).get_json()
    assert r.get("ok"), r
    return r["id"]


# --- Seed library: four topics, each with a few clips, plus a bulk tag that
# rides along on everything (the kind that used to drown out specifics). ---
PY = ("asyncio event loop coroutines await async def tasks gather futures "
      "concurrency non-blocking IO in Python %s")
for i, extra in enumerate(["streams and protocols", "timeouts and cancellation",
                           "task groups and structured concurrency"]):
    add(f"Python asyncio {i}", PY % extra,
        tags=["python", "asyncio", "bulk-import"],
        url=f"https://docs.python.org/async/{i}")

K8S = ("kubernetes pods deployments services ingress kubectl cluster nodes "
       "container orchestration helm namespaces %s")
for i, extra in enumerate(["rolling updates and probes", "network policies and CNI",
                           "horizontal pod autoscaling"]):
    add(f"Kubernetes {i}", K8S % extra,
        tags=["kubernetes", "devops", "bulk-import"],
        url=f"https://k8s.example.io/guide/{i}")

REEF = ("coral reef bleaching ocean warming sea temperature marine ecosystem "
        "symbiotic algae zooxanthellae biodiversity %s")
for i, extra in enumerate(["the great barrier reef", "acidification and pH",
                           "restoration and coral gardening"]):
    add(f"Coral reefs {i}", REEF % extra,
        tags=["climate", "marine-biology", "bulk-import"],
        url=f"https://reef.example.org/{i}")

# Pure filler carrying only the bulk tag.
for i in range(6):
    add(f"Misc {i}", f"office stationery and printer toner inventory notes {i}",
        tags=["bulk-import"])


def show(label, **kw):
    conn = connect()
    sug = SG.suggest_tags(conn, **kw)
    conn.close()
    print(f"\n### {label}")
    if "chosen_tags" in kw and kw["chosen_tags"]:
        print(f"    (already chosen: {kw['chosen_tags']})")
    if not sug:
        print("    (no confident suggestions)")
    for s in sug:
        flag = WHY.get(s["why"], s["why"])
        print(f"    {s['score']:>5.2f}  {s['tag']:<28} {flag}")


# 1. On-topic article matching an existing topic.
show("Article about Python async/await",
     title="Understanding async/await in Python",
     text="A guide to writing coroutines with async def and awaiting futures. "
          "The event loop schedules tasks; gather runs them concurrently "
          "without blocking IO. Covers cancellation and timeouts.")

# 2. Another existing topic, via the body only.
show("Article about Kubernetes networking",
     text="How ingress controllers route traffic to services inside a "
          "kubernetes cluster, and how network policies restrict pod-to-pod "
          "traffic across namespaces with kubectl.")

# 3. Same as #1 but the user already picked 'python' -> co-occurrence.
show("Python async article, 'python' already chosen",
     title="Understanding async/await in Python",
     text="Coroutines, the event loop, gather and concurrent tasks.",
     chosen_tags=["python"])

# 4. A genuinely novel topic: nothing in the library is close, so the only
# useful output is freshly extracted keyphrase tags.
show("Novel article: home espresso",
     title="Dialing in espresso at home",
     text="Dialing in espresso means adjusting grind size, dose and shot "
          "time to balance extraction. A burr grinder, a calibrated tamper "
          "and fresh beans matter more than the machine. Watch the pour for "
          "channeling and aim for a steady ristretto extraction.")

print("\n(legend: ≈ similar  ◎ predicted  ↔ co-occurs  "
      "★ multiple  ＋ NEW tag)")
