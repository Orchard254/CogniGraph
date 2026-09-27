#!/usr/bin/env python3
"""
CLI utility for CogniGraph.
Allows human developers or scripts to inspect, audit, query, export, link graph edges, verify code provenance, and consolidate project memory.
"""
import sys
import argparse
import json
from pathlib import Path
from memory_store import MemoryStore, default_db_path


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="CogniGraph CLI Utility")
    parser.add_argument("--db", default=None, help="Path to SQLite database file")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # stats
    stats_p = subparsers.add_parser("stats", help="Show database statistics and category breakdown")
    stats_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # audit
    audit_p = subparsers.add_parser("audit", help="Run conflict and duplicate detection audit")
    audit_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    # SUPPRESS so an unsupplied flag is absent from the namespace and the store
    # call stays audit() with no kwargs -- callers that stub audit() keep working.
    audit_p.add_argument("--semantic-threshold", type=float, default=argparse.SUPPRESS,
                         help="Cosine cutoff for embedded fact pairs (default 0.80)")
    audit_p.add_argument("--lexical-threshold", type=float, default=argparse.SUPPRESS,
                         help="Jaccard cutoff for facts with no embedding (default 0.65)")
    # SUPPRESS so an unsupplied flag is absent from the namespace and the store
    # call stays audit() with no kwargs -- callers that stub audit() keep working.
    audit_p.add_argument("--disagreements-only", action="store_true",
                         default=argparse.SUPPRESS,
                         help="show only pairs whose facts CONTRADICT each other")

    pollution_p = subparsers.add_parser(
        "pollution", help="Report throwaway test/stress artifacts (read-only)")
    pollution_p.add_argument("--db", default=argparse.SUPPRESS,
                              help="Path to SQLite database file")

    locate_p = subparsers.add_parser(
        "locate", help="Show where a fact id lives and whether it is live")
    locate_p.add_argument("fact_id", type=int, help="fact id to locate")
    locate_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # export
    export_parser = subparsers.add_parser("export", help="Export active memory as Markdown")
    export_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    export_parser.add_argument("--category", help="Category filter")
    export_parser.add_argument("--out", help="Output file path")

    # query
    query_parser = subparsers.add_parser("query", help="Query active facts by keyword")
    query_parser.add_argument("text", help="Search keywords")
    query_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    query_parser.add_argument("--category", help="Category filter")
    query_parser.add_argument("--tag", help="Tag filter")
    query_parser.add_argument("--hybrid", action="store_true", help="Use hybrid RRF query")
    query_parser.add_argument("--limit", type=int, default=10, help="Max results")

    # link
    link_parser = subparsers.add_parser("link", help="Link two facts with a directed graph edge")
    link_parser.add_argument("source_id", type=int, help="Source fact ID")
    link_parser.add_argument("target_id", type=int, help="Target fact ID")
    link_parser.add_argument("relation", choices=["depends_on", "implements", "conflicts_with", "relates_to"], help="Relationship type")
    link_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # graph
    graph_parser = subparsers.add_parser("graph", help="Traverse knowledge graph from root_id")
    graph_parser.add_argument("root_id", type=int, help="Root fact ID")
    graph_parser.add_argument("--depth", type=int, default=3, help="Max traversal depth")
    graph_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # verify
    verify_parser = subparsers.add_parser("verify", help="Verify code snippet provenance at file_path")
    verify_parser.add_argument("fact_id", type=int, help="Fact ID to verify")
    verify_parser.add_argument("--root", help="Project root directory")
    verify_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # consolidate
    cons_parser = subparsers.add_parser("consolidate", help="Consolidate state facts into milestone summary")
    cons_parser.add_argument("--session", help="Session ID filter")
    cons_parser.add_argument("--category", default="state", help="Category to consolidate")
    cons_parser.add_argument("--all", action="store_true", help="Explicitly allow consolidating ALL facts (no session filter)")
    cons_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # context
    context_parser = subparsers.add_parser("context", help="Get rich context block with graph dependencies for a query")
    context_parser.add_argument("query", help="Target question or topic")
    context_parser.add_argument("--limit", type=int, default=5, help="Max primary facts")
    context_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # file-context
    file_context_parser = subparsers.add_parser("file-context", help="Get file-centric impact briefing before modifying a file")
    file_context_parser.add_argument("file_path", help="Path to target source file")
    file_context_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # check-safety
    safety_parser = subparsers.add_parser("check-safety", help="Run multi-file refactor safety matrix")
    safety_parser.add_argument("files", nargs="+", help="List of file paths to check")
    safety_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # optimize
    optimize_parser = subparsers.add_parser("optimize", help="Run SQLite vacuum and index optimization")
    optimize_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # export-ui
    ui_parser = subparsers.add_parser("export-ui", help="Export standalone interactive HTML graph visualizer")
    ui_parser.add_argument("--out", help="Output HTML file path (default: .cognigraph/memory_graph.html)")
    ui_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # state
    state_parser = subparsers.add_parser("state", help="Get active memory state snapshot")
    state_parser.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    state_parser.add_argument("--category", help="Category filter")
    state_parser.add_argument("--json", action="store_true", help="Output raw JSON")

    # scan-provenance
    scan_p = subparsers.add_parser("scan-provenance", help="Bulk provenance health check across all anchored facts")
    scan_p.add_argument("--root", help="Project root directory to resolve relative paths")
    scan_p.add_argument("--auto-deprecate", action="store_true", help="Auto-deprecate facts whose files were deleted")
    scan_p.add_argument("--auto-heal", action="store_true", help="Auto-update code_hash for low-importance drifted facts")
    scan_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # backfill
    backfill_p = subparsers.add_parser("backfill", help="Generate embeddings for active facts missing them")
    backfill_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # backup
    backup_p = subparsers.add_parser("backup", help="Timestamped full-DB snapshot; prunes beyond --keep")
    backup_p.add_argument("--tag", default="manual", help="Backup label (default: manual)")
    backup_p.add_argument("--keep", type=int, default=8, help="Timestamped backups to retain (default 8)")
    backup_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # promote
    promote_p = subparsers.add_parser("promote", help="Promote an experimental branch's episodic memories into canonical (KEEP_BOTH)")
    promote_p.add_argument("source_branch_id", type=int, help="Source experimental branch ID to promote")
    promote_p.add_argument("--policy", default="KEEP_BOTH", help="Convergence policy (only KEEP_BOTH implemented)")
    promote_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # branch
    branch_p = subparsers.add_parser("branch", help="Branch lifecycle commands")
    branch_sub = branch_p.add_subparsers(dest="branch_command", required=True)
    branch_create_p = branch_sub.add_parser("create", help="Create a new experimental branch under the canonical root")
    branch_create_p.add_argument("--repo", type=int, default=None, help="Repo ID (defaults to single-repo derivation)")
    branch_create_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    branch_discard_p = branch_sub.add_parser("discard", help="Discard an experimental branch (terminal, with tombstone)")
    branch_discard_p.add_argument("source_branch_id", type=int, help="Source experimental branch ID to discard")
    branch_discard_p.add_argument("--reason", default=None, help="Optional discard rationale")
    branch_discard_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")
    branch_list_p = branch_sub.add_parser("list", help="List branches for a repo (read-only)")
    branch_list_p.add_argument("--repo", type=int, default=None, help="Repo ID (defaults to single-repo derivation)")
    branch_list_p.add_argument("--status", choices=["ACTIVE", "PROMOTED", "DISCARDED"], default=None, help="Status filter")
    branch_list_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # prune
    prune_p = subparsers.add_parser("prune", help="Deprecate stale low-value facts (dry-run by default)")
    prune_p.add_argument("--days", type=int, default=90, help="Days of inactivity before pruning")
    prune_p.add_argument("--apply", action="store_true", help="Actually prune (default is dry-run preview)")
    prune_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # prepare-task
    prep_p = subparsers.add_parser("prepare-task", help="Pre-coding flight briefing for a task")
    prep_p.add_argument("intent", help="Short description of the task")
    prep_p.add_argument("--files", nargs="*", default=[], help="Files planned for editing")
    prep_p.add_argument("--limit", type=int, default=5, help="Max primary facts")
    prep_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # bootstrap
    boot_p = subparsers.add_parser("bootstrap", help="Onboard a repository into memory (structure, docs, manifests, git)")
    boot_p.add_argument("--root", help="Project root to bootstrap (default: CWD)")
    boot_p.add_argument("--apply", action="store_true", help="Actually write facts (default is dry-run preview)")
    boot_p.add_argument("--full", action="store_true", help="Per-file facts for all code files (use carefully on large repos)")
    boot_p.add_argument("--max-facts", type=int, default=150, help="Cap on facts to ingest")
    boot_p.add_argument("--no-hook", action="store_true", help="Skip git post-commit hook installation")
    boot_p.add_argument("--no-protocol", action="store_true", help="Skip AGENTS.md drop")
    boot_p.add_argument("--force-protocol", action="store_true", help="Back up and overwrite a customized AGENTS.md instead of staging AGENTS.md.new")
    boot_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    # wire
    wire_p = subparsers.add_parser("wire", help="Generate MCP config for a coding agent on a target project")
    wire_p.add_argument("agent", nargs="?", default=None,
                        help="Target coding agent (antigravity, cursor, windsurf, claude, zed, roo, cline, opencode, kiro, vscode, zcode, generic). Omit for interactive menu.")
    wire_p.add_argument("--root", help="Project root to wire (default: CWD)")
    wire_p.add_argument("--no-protocol", action="store_true", help="Skip AGENTS.md drop")
    wire_p.add_argument("--force-protocol", action="store_true", help="Back up and overwrite a customized AGENTS.md instead of staging AGENTS.md.new")
    wire_p.add_argument("--global", dest="global_config", action="store_true",
                        help="Also update the user-global config (antigravity only; default is project-local)")
    wire_p.add_argument("--db", default=argparse.SUPPRESS, help="Path to SQLite database file")

    args = parser.parse_args()
    # One-shot legacy (.project-memory -> .cognigraph) data-dir migration at
    # CLI entry — never per command internals (os.replace on an open SQLite
    # file fails on Windows). An explicit --db bypasses CWD/env sniffing;
    # an explicit --root migrates just that project.
    from pm_common import ensure_startup_migration, maybe_migrate_legacy_data_dir
    if getattr(args, "db", None) or getattr(args, "root", None):
        hints = [args.root] if getattr(args, "root", None) else []
        ensure_startup_migration(hints=hints, include_cwd=False, include_env=False)
    else:
        ensure_startup_migration()
    # Without explicit --db, bind to target project's memory DB if --root given
    # so facts never land in the CWD fallback store (and no stray folder is created).
    db_path = getattr(args, "db", None)
    if db_path is None and getattr(args, "root", None):
        db_path = str(maybe_migrate_legacy_data_dir(Path(args.root).resolve()) / "memory.db")
    db_path = db_path or default_db_path()

    # Constructed lazily per command: `wire` never touches the DB, and
    # constructing a store can create the DB file as a side effect.
    store = None
    if args.command != "wire":
        store = MemoryStore(db_path)
    try:
        if args.command == "stats":
            stats = store.stats()
            print("=== CogniGraph Statistics ===")
            print(f"Database Path    : {stats['db_path']}")
            print(f"Total Active     : {stats['total_active']}")
            print(f"  Curated        : {stats.get('active_curated', 'n/a')}")
            print(f"  Template Stubs : {stats.get('active_stubs', 'n/a')}")
            print(f"Active post-genesis (live)  : {stats.get('active_post_genesis_count', stats.get('active_post_genesis', 0))}  (memory table is the live intake store by contract)")
            print(f"Genesis migrated : {stats.get('genesis_migrated', 0)}")
            print(f"Episodic Active  : {stats.get('episodic_active', 0)}")
            print(f"Total Deprecated : {stats['total_deprecated']}")
            print(f"Total Edges      : {stats['total_edges']}")
            print(f"Embedded Facts     : {stats.get('embedded_facts', 0)}")
            print(f"Semantic Backend   : {stats.get('semantic_backend', False)}")
            print("\nActive Facts by Category:")
            for cat, count in stats['active_by_category'].items():
                print(f"  - {cat:12s}: {count}")

        elif args.command == "audit":
            audit_kwargs = {}
            for opt in ("semantic_threshold", "lexical_threshold", "disagreements_only"):
                if hasattr(args, opt):
                    audit_kwargs[opt] = getattr(args, opt)
            res = store.audit(**audit_kwargs)
            print("=== CogniGraph Audit ===")
            print(f"Total Active Checked : {res['total_active_checked']}")
            if res.get("pair_verdicts"):
                v = res["pair_verdicts"]
                print(f"Pairs Examined       : {res['pairs_examined']}  (disagree "
                      f"{v['disagree']}, agree {v['agree']}, unclear {v['unclear']})")
            print(f"Potential Duplicates : {res['potential_duplicates_count']}")
            if res.get("thresholds"):
                th = res["thresholds"]
                print(f"Thresholds           : semantic>={th['semantic']} lexical>={th['lexical']}"
                      f" (method: {res.get('method', 'n/a')}, {res.get('embedded_checked', 0)} embedded)")
            if res["potential_duplicates"]:
                print("\nFlagged Candidates:")
                print("  (read-only report -- reconcile with memory_supersede, nothing was changed)")
                for item in res["potential_duplicates"]:
                    cross = "  CROSS-CATEGORY" if item.get("cross_category") else ""
                    print(f"\nSimilarity: {item['similarity']} [{item['category']}]"
                          f" via {item.get('method', '?')}"
                          f"  VERDICT={item.get('verdict', '?')}{cross}")
                    for c in item.get("conflicts", []):
                        print(f"  !! CONFLICT {c['unit']}: {c['a_value']} vs {c['b_value']}"
                              f"  ({c['kind']})")
                    for n in (1, 2):
                        print(f"  ID {item[f'fact{n}_id']} [{item.get(f'fact{n}_category', '?')}]"
                              f" {item.get(f'fact{n}_file_path') or '-'}: {item[f'fact{n}_content']}")
            else:
                print("No duplicate active facts detected.")

        elif args.command == "pollution":
            res = store.detect_pollution()
            print("=== Pollution Scan (read-only) ===")
            print(f"Scanned              : {res['scanned']} boosted-category active facts")
            print(f"Flagged              : {res['count']}")
            if res["pollution"]:
                print(f"\n  {res['note']}")
                for item in res["pollution"]:
                    print(f"\n  #{item['id']} [{item['category']}] {item['reason']}")
                    print(f"    {item['content'][:100]}")
                    if item["rationale"]:
                        print(f"    rationale: {item['rationale'][:80]}")
            else:
                print("No pollution detected.")

        elif args.command == "locate":
            res = store.locate_fact(args.fact_id)
            print(f"=== Fact {args.fact_id} ===")
            print(f"Liveness        : {res['liveness']}  (authority: {res['liveness_authority']})")
            print(f"In `memory`     : {res['in_memory']}")
            if res["memory_deprecated_at"]:
                print(f"  deprecated_at : {res['memory_deprecated_at']}")
                print(f"  reason        : {res['memory_deprecated_reason']}")
            if res["category"]:
                print(f"  category      : {res['category']}")
            print(f"Episodic rows   : {len(res['episodic'])}")
            for e in res["episodic"]:
                print(f"  branch {e['branch_id']} repo {e['repo_id']} status {e['status']}")
            if res["episodic_active_but_not_served"]:
                print("  ** ACTIVE in episodic but DEPRECATED in memory -> NOT served **")
            print(f"Graph edges     : {len(res['edges'])}")
            for e in res["edges"]:
                print(f"  {e['source_id']} [{e['relation']}] -> {e['target_id']}")

        elif args.command == "export":
            md = store.export_markdown(category=args.category)
            if args.out:
                out_path = Path(args.out)
                if out_path.parent != Path("."):
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(md, encoding="utf-8")
                print(f"Exported memory to {args.out}")
            else:
                print(md)

        elif args.command == "query":
            limit = max(1, min(100, args.limit or 10))
            if args.hybrid:
                # Deliberately logs usage (default log_usage=True): a CLI
                # query is user-initiated retrieval, exactly what usage
                # learning should learn from (#436 allowlist).
                results = store.query_hybrid(args.text, category=args.category, tag=args.tag, limit=limit)
            else:
                results = store.query(args.text, category=args.category, tag=args.tag, limit=limit)
            print(f"Found {len(results)} matching facts:")
            for r in results:
                print(f"[{r['id']}] ({r['category']}) {r['content']}")
                if r['file_path']:
                    print(f"     file: {r['file_path']}")

        elif args.command == "link":
            edge_id = store.link_facts(args.source_id, args.target_id, args.relation)
            print(f"Linked fact {args.source_id} -[{args.relation}]-> fact {args.target_id} (edge ID: {edge_id})")

        elif args.command == "graph":
            depth = max(1, min(10, args.depth or 3))
            edges = store.get_graph(args.root_id, max_depth=depth)
            print(f"=== Knowledge Graph from Fact {args.root_id} ({len(edges)} edges) ===")
            for e in edges:
                indent = "  " * e['depth']
                print(f"{indent}- [{e['relation']}] -> [{e['target_id']}] ({e['target_category']}) {e['target_content']}")

        elif args.command == "verify":
            res = store.verify_provenance(args.fact_id, project_root=args.root)
            print(f"Provenance status for fact {args.fact_id}: {res['status']}")
            print(json.dumps(res, indent=2))

        elif args.command == "consolidate":
            res = store.consolidate_state_facts(session_id=args.session, category=args.category, allow_all=args.all)
            print("Consolidation result:", json.dumps(res, indent=2))

        elif args.command == "context":
            ctx = store.get_context(args.query, limit=max(1, min(50, args.limit or 5)))
            print(ctx)

        elif args.command == "file-context":
            briefing = store.get_file_context(args.file_path)
            print(briefing)

        elif args.command == "check-safety":
            res = store.check_safety(args.files)
            print(json.dumps(res, indent=2))

        elif args.command == "optimize":
            res = store.optimize_db()
            print("Database Optimization Complete:", json.dumps(res, indent=2))

        elif args.command == "export-ui":
            html_path = store.export_interactive_html(args.out)
            print(f"Exported interactive knowledge graph UI to {html_path}")

        elif args.command == "state":
            # limit=None: this command labels its output "Active State (N facts)",
            # so N must be the true active count. The 200 default made it report
            # 200 while hundreds more existed.
            facts = store.get_state(category=args.category, limit=None)
            if args.json:
                print(json.dumps(facts, indent=2))
            else:
                print(f"=== Active State ({len(facts)} facts) ===")
                for f in facts:
                    print(f"[{f['id']}] ({f['category']}) [{f['confidence']}] {f['content']}")
        elif args.command == "scan-provenance":
            res = store.scan_provenance(project_root=args.root, auto_deprecate=args.auto_deprecate,
                                        auto_heal=getattr(args, "auto_heal", False))
            print(json.dumps(res, indent=2))
        elif args.command == "backfill":
            res = store.backfill_embeddings()
            print(json.dumps(res, indent=2))
        elif args.command == "backup":
            res = store.backup_db(tag=args.tag or "manual", keep=max(1, args.keep or 8))
            print(json.dumps(res, indent=2))
        elif args.command == "promote":
            from branch_promotion import promote_branch
            res = promote_branch(db_path, args.source_branch_id, convergence_policy=args.policy)
            print(json.dumps(res, indent=2))
        elif args.command == "branch":
            if args.branch_command == "create":
                from branch_promotion import create_branch
                res = create_branch(db_path, repo_id=args.repo)
                print(json.dumps(res, indent=2))
            elif args.branch_command == "discard":
                from branch_promotion import discard_branch
                res = discard_branch(db_path, args.source_branch_id, reason=args.reason)
                print(json.dumps(res, indent=2))
            elif args.branch_command == "list":
                from branch_promotion import list_branches
                res = list_branches(db_path, repo_id=args.repo, status=args.status)
                print(json.dumps(res, indent=2))
        elif args.command == "prune":
            res = store.prune_stale(days_inactive=max(1, args.days or 90), dry_run=not args.apply)
            print(json.dumps(res, indent=2))
        elif args.command == "prepare-task":
            print(store.prepare_task(args.intent, file_paths=args.files or None,
                                     limit=max(1, min(50, args.limit or 5))))
        elif args.command == "bootstrap":
            from bootstrap import bootstrap_project
            res = bootstrap_project(args.root, store=store, dry_run=not args.apply,
                                    full=args.full, max_facts=max(1, min(1000, args.max_facts or 150)),
                                    install_hook=not args.no_hook,
                                    drop_protocol=not args.no_protocol,
                                    force_protocol=getattr(args, "force_protocol", False))
            print(json.dumps(res, indent=2))
        elif args.command == "wire":
            from wire import wire_agent, detect_agent, prompt_agent_choice, SUPPORTED_AGENTS, AGENT_ALIASES
            agent = args.agent
            if agent is None:
                detected = detect_agent(Path(args.root) if args.root else None)
                if sys.stdin.isatty():
                    agent = prompt_agent_choice(detected)
                elif detected:
                    sys.stderr.write(f"Auto-detected IDE/agent: {detected}\n")
                    agent = detected
                else:
                    sys.stderr.write(
                        f"Error: No agent specified and could not auto-detect. "
                        f"Choose one of: {', '.join(SUPPORTED_AGENTS)}\n"
                    )
                    sys.exit(1)
            else:
                agent_norm = AGENT_ALIASES.get(agent.lower().strip(), agent.lower().strip())
                if agent_norm not in SUPPORTED_AGENTS:
                    sys.stderr.write(
                        f"Error: Invalid agent '{agent}'. "
                        f"Choose one of: {', '.join(SUPPORTED_AGENTS)}\n"
                    )
                    sys.exit(1)
                agent = agent_norm
            try:
                res = wire_agent(agent, project_root=args.root,
                                 drop_protocol=not args.no_protocol,
                                 global_config=getattr(args, "global_config", False),
                                 force_protocol=getattr(args, "force_protocol", False))
                print(json.dumps(res, indent=2))
            except ValueError as e:
                sys.stderr.write(f"Error: {e}\n")
                sys.exit(1)

    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    main()
