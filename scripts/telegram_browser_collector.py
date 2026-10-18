"""Standalone CLI. Run: .venv\\Scripts\\python.exe -m scripts.telegram_browser_collector auth"""
import argparse
import json
import sys

from scripts.collector.browser import open_browser, wait_for_session
from scripts.collector.config import DEFAULT_CONFIG, load_config, runtime_root
from scripts.collector.store import CollectorStore


def result_exit_code(state):
    return 0 if state in ('AUTHENTICATED','COMPLETE','PASS','COMPLETED','DRY_RUN','EMPTY') else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["auth", "status", "check", "collect", "diagnose", "pipeline",'events','content','handoff','recover'])
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-seconds", type=int, default=900)
    parser.add_argument("--source", default="saved")
    parser.add_argument("--message-id")
    parser.add_argument('--version',type=int)
    parser.add_argument('--event-key')
    parser.add_argument('--live-test',action='store_true',help='One explicit own text event through existing Hermes CLI; default is dry-run')
    parser.add_argument('--recheck-media',action='store_true')
    parser.add_argument('--metadata-only',action='store_true')
    args = parser.parse_args()
    config = load_config(args.config)
    root = runtime_root()
    from scripts.collector.browser import protect_runtime
    protect_runtime(root)
    store = CollectorStore(root / "collector.sqlite3")
    if args.command == "status":
        print(json.dumps(store.status(), ensure_ascii=True, indent=2))
        return 0
    try:
        if args.command=='events':
            result={'state':'COMPLETE','events':store.events()}
        elif args.command=='recover':
            result={'state':'COMPLETE','needs_review':store.recover_events()}
        elif args.command=='content':
            item=store.get(args.source,args.message_id,args.version)
            result={'state':'COMPLETE' if item else 'NOT_FOUND','material':item}
        elif args.command=='handoff':
            from scripts.collector.handoff import dispatch
            result=dispatch(store,root,dry_run=not args.live_test,event_key=args.event_key)
        elif args.command == "pipeline":
            from scripts.collector.pipeline import run_pipeline
            result = run_pipeline(store, root, args.source, args.message_id)
        else:
            with open_browser(root, args.headless) as (context, page):
                state = wait_for_session(page,
                    max(1, min(args.wait_seconds, 1800)) if args.command == "auth" else 30,
                    allow_login=args.command == "auth")
                if state != "AUTHENTICATED":
                    result = {"state": state}
                elif args.command in ("auth", "diagnose"):
                    result = {"state": state, "ui": "Telegram Web A", "dedicated_profile": True}
                else:
                    from scripts.collector.telegram import run_sources
                    result = run_sources(page, store, root, config, args)
        store.record_run(args.command, result["state"], result)
        print(json.dumps(result, ensure_ascii=True, indent=2), flush=True)
        return result_exit_code(result['state'])
    except Exception as exc:
        # Exceptions may include DOM text, filesystem paths or full browser call logs.
        # Persist only a class and controlled code, never raw exception text.
        code = str(exc) if str(exc) in ("PROFILE_BUSY", "IDENTITY_MISMATCH", "SOURCE_UNAVAILABLE") else type(exc).__name__
        result = {"state": "ERROR", "code": code}
        store.record_run(args.command, "ERROR", result)
        print(json.dumps(result), flush=True)
        return 3


if __name__ == "__main__":
    sys.exit(main())
