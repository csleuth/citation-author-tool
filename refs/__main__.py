"""CLI: python -m refs <command> ...  Only `app --live` can edit Wikipedia, and only edits you approve."""
import argparse
from . import decide, discover, edit, fetch, label, read, scan


def main():
    ap = argparse.ArgumentParser(prog="refs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover"); p.add_argument("periods", nargs="+", help="YYYY/MM or YYYY/MM/DD")
    sub.add_parser("scan")
    p = sub.add_parser("sample"); p.add_argument("name"); p.add_argument("--n", type=int, required=True)
    p.add_argument("--seed", type=int, required=True); p.add_argument("--exclude")
    for cmd in ("fetch", "read", "decide", "fetch-alt"):
        p = sub.add_parser(cmd); p.add_argument("sample"); p.add_argument("--run", required=True)
        if cmd == "read":
            p.add_argument("--fresh", action="store_true", help="ignore cached LLM answers (stability rerun)")
    p = sub.add_parser("export-cases"); p.add_argument("sample"); p.add_argument("--run", required=True)
    p = sub.add_parser("compare"); p.add_argument("sample"); p.add_argument("--run", required=True)
    p = sub.add_parser("import-labels"); p.add_argument("csv"); p.add_argument("--run", required=True)
    p = sub.add_parser("stability"); p.add_argument("sample"); p.add_argument("run1"); p.add_argument("run2")
    p = sub.add_parser("dryrun", help="build exact new wikitext for HIGH proposals; never posts")
    p.add_argument("sample"); p.add_argument("--run", required=True)
    p = sub.add_parser("app", help="review app; dry run unless --live")
    p.add_argument("sample", help="the sample whose gold set (<sample>-gold, from export-cases) is reviewed")
    p.add_argument("--run", required=True, help="the run whose proposals are reviewed")
    p.add_argument("--live", action="store_true", help="really post approved edits to Wikipedia")
    p.add_argument("--max-live", type=int, default=None, help="optional: stop after this many live article edits in total (default: no limit)")
    p.add_argument("--port", type=int, default=4580)
    sub.add_parser("whoami", help="log in (no edit) and show the account's groups")
    sub.add_parser("check-edits", help="read-only: were sent edits reverted, and are the added names still there?")
    p = sub.add_parser("report"); p.add_argument("sample"); p.add_argument("--run", required=True)
    a = ap.parse_args()
    if a.cmd == "discover": discover.discover(a.periods)
    elif a.cmd == "scan": scan.scan()
    elif a.cmd == "sample": fetch.draw_sample(a.name, a.n, a.seed, a.exclude)
    elif a.cmd == "fetch": fetch.fetch(a.sample, a.run)
    elif a.cmd == "read": read.read(a.sample, a.run, fresh=a.fresh)
    elif a.cmd == "fetch-alt": fetch.fetch_alternates(a.sample, a.run)
    elif a.cmd == "decide": decide.decide(a.sample, a.run)
    elif a.cmd == "export-cases": label.export_cases(a.sample, a.run)
    elif a.cmd == "compare": label.compare(a.sample, a.run)
    elif a.cmd == "import-labels": label.import_labels(a.csv, a.run)
    elif a.cmd == "stability": label.stability(a.sample, a.run1, a.run2)
    elif a.cmd == "dryrun": edit.dryrun(a.sample, a.run)
    elif a.cmd == "app":
        from . import app as review_app
        review_app.run(a.sample, a.run, live=a.live, max_live=a.max_live, port=a.port)
    elif a.cmd == "whoami":
        from . import wiki
        wiki.whoami()
    elif a.cmd == "report": label.report(a.sample, a.run)
    elif a.cmd == "check-edits":
        from . import check_edits
        check_edits.run_check()


if __name__ == "__main__":
    main()
