# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "garminconnect>=0.3.6",
#     "psycopg[binary]>=3.3",
#     "rich>=13.0",
#     "questionary>=2.0",
#     "requests>=2.31",
#     "python-dotenv>=1.0",
# ]
# ///
"""
Guided first-time setup (and re-auth) for garmin-data.

    uv run install.py                     # full first-time setup
    uv run install.py --reauth            # just re-log-in to Garmin and push the
                                           # refreshed token to the DB (e.g. after a
                                           # Garmin password change) — no schema/
                                           # secrets/backfill steps.
    uv run install.py --upgrade           # adopt/upgrade an existing install: apply
                                           # pending migrations, add/remove domains,
                                           # trigger a targeted backfill for newly
                                           # enabled domains.
    uv run install.py --domains a,b,c     # non-interactive domain selection for
                                           # setup or --upgrade (also settable via
                                           # GARMIN_DOMAINS).
    uv run install.py --coach-loop        # opt in to the coach loop (creates two
                                           # scoped DB roles; grants a component
                                           # Garmin write access).

Everything here is local orchestration of steps you could also do by hand
(see README.md) — it doesn't introduce any new credential storage.
"""

import argparse
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import psycopg
import questionary
from rich.console import Console
from rich.panel import Panel

from apply_schema import apply_schema, apply_migrations, applied_versions, list_migrations
from bootstrap.garmin_auth import interactive_login, save_token_to_db
from coach_roles import (
    CoachLoopRoles, apply_coach_grants, assert_role_isolated, ensure_role,
    generate_password, role_exists, role_of_url, url_for_role,
)
from config import get_coach_loop, get_enabled_domains, set_coach_loop, set_enabled_domains
from domains import DOMAINS, DOMAINS_BY_KEY, Domain
from protocols import seed_default_protocols

console = Console()

ENV_PATH = pathlib.Path(__file__).resolve().parent / ".env"

_POSTGRES_URL_RE = re.compile(r"^postgres(?:ql)?://")


def _read_existing_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"')
    return values


def _write_env(values: dict[str, str]) -> None:
    existing = _read_existing_env()
    existing.update(values)
    lines = [f'{k}="{v}"' for k, v in existing.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n")
    for k, v in values.items():
        os.environ[k] = v


def _prompt_db_url(label: str, env_key: str, existing: dict[str, str]) -> str:
    default = existing.get(env_key, "")
    while True:
        value = questionary.password(f"{label} (postgresql://...):", default=default).ask()
        if value is None:
            console.print("[red]Setup cancelled.[/red]")
            sys.exit(1)
        value = value.strip()
        if _POSTGRES_URL_RE.match(value):
            return value
        console.print("[red]That doesn't look like a postgres connection string — try again.[/red]")


def _check_connection(url: str, label: str) -> None:
    try:
        with psycopg.connect(url, connect_timeout=10) as conn:
            conn.execute("select 1")
    except Exception as e:
        console.print(Panel(f"Couldn't connect using {label}: {e}", title="✗ Connection failed", border_style="red"))
        sys.exit(1)
    console.print(f"[green]✓[/green] Connected using {label}.")


def _detect_github_repo() -> str | None:
    if shutil.which("gh"):
        try:
            out = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner"],
                capture_output=True, text=True, timeout=10, check=True,
            )
            import json as _json
            return _json.loads(out.stdout)["nameWithOwner"]
        except Exception:
            pass
    try:
        out = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=10, check=True,
        )
        url = out.stdout.strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _gh_available() -> tuple[bool, str]:
    if not shutil.which("gh"):
        return False, (
            "The `gh` CLI isn't installed — can't set repo secrets or trigger the "
            "backfill automatically. Install it (https://cli.github.com), run "
            "`gh auth login`, then re-run this wizard — or follow the manual "
            "instructions below."
        )
    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False, (
            "`gh` is installed but not authenticated — run `gh auth login`, then "
            "re-run this wizard. Or follow the manual instructions below."
        )
    return True, ""


def _set_gh_secret(repo: str, name: str, value: str) -> None:
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", repo, "--body", value],
        check=True, capture_output=True, text=True,
    )


def _print_manual_secret_instructions(database_url: str) -> None:
    console.print(Panel(
        "Set this repo secret by hand:\n\n"
        "  1. On GitHub, go to your repo → [bold]Settings[/bold] → "
        "[bold]Secrets and variables[/bold] → [bold]Actions[/bold] → "
        "[bold]New repository secret[/bold]\n"
        f"  2. Name: [bold]NEON_DATABASE_URL[/bold]\n"
        f"  3. Value: {database_url}\n\n"
        "Then trigger the initial backfill by hand: [bold]Actions[/bold] tab → "
        "[bold]garmin-backfill[/bold] → [bold]Run workflow[/bold].",
        title="Manual GitHub Actions setup", border_style="yellow",
    ))


def _parse_domains_arg(raw: str) -> set[str]:
    keys = {k.strip() for k in raw.split(",") if k.strip()}
    unknown = keys - DOMAINS_BY_KEY.keys()
    if unknown:
        console.print(
            f"[red]Unknown domain(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(d.key for d in DOMAINS)}[/red]"
        )
        sys.exit(1)
    return keys


def _prompt_domain_checklist(preselected: set[str]) -> set[str]:
    choices = []
    last_category = None
    for d in DOMAINS:
        if d.category != last_category:
            choices.append(questionary.Separator(f"-- {d.category} --"))
            last_category = d.category
        choices.append(questionary.Choice(
            title=f"{d.label} — {d.description}",
            value=d.key,
            checked=d.key in preselected,
        ))
    selected = questionary.checkbox("Which data domains should this sync?", choices=choices).ask()
    if selected is None:
        console.print("[red]Setup cancelled.[/red]")
        sys.exit(1)
    return set(selected)


def _pending_migrations(database_url: str, enabled_domains: set[str]) -> list[str]:
    applied = applied_versions(database_url)
    return [
        name for version, domain, name in list_migrations()
        if version not in applied and (domain == "base" or domain in enabled_domains)
    ]


def _print_whats_new(newly_available: list[Domain], pending_names: list[str]) -> None:
    if not newly_available and not pending_names:
        console.print("[green]Nothing new — you're fully up to date.[/green]")
        return
    lines = []
    if newly_available:
        lines.append("[bold]New domains available:[/bold]")
        for d in newly_available:
            lines.append(f"  • {d.label} ({d.key}) — {d.description}")
    if pending_names:
        if lines:
            lines.append("")
        lines.append("[bold]New data pending in domains you already have:[/bold]")
        for name in pending_names:
            lines.append(f"  • {name}")
    console.print(Panel("\n".join(lines), title="What's new", border_style="cyan"))


def _load_env_into_os(existing: dict) -> None:
    for key in ("GITHUB_TOKEN", "GITHUB_REPO"):
        if key in existing:
            os.environ[key] = existing[key]


def _trigger_targeted_backfill(existing: dict, domains: set[str]) -> None:
    console.print(f"\n[bold]Triggering targeted backfill for: {', '.join(sorted(domains))}[/bold]")
    gh_ok, gh_message = _gh_available()
    have_pat = "GITHUB_TOKEN" in existing and "GITHUB_REPO" in existing
    if not gh_ok or not have_pat:
        reason = gh_message if not gh_ok else "No GitHub PAT/repo on file from a previous setup run."
        console.print(f"[yellow]![/yellow] {reason}")
        console.print(Panel(
            "Trigger it by hand: [bold]Actions[/bold] tab → [bold]garmin-backfill[/bold] → "
            f"[bold]Run workflow[/bold], with domains = {','.join(sorted(domains))}",
            title="Manual backfill trigger", border_style="yellow",
        ))
        return

    _load_env_into_os(existing)
    from workflow_tools import trigger_backfill  # lazy: needs GITHUB_TOKEN/GITHUB_REPO in os.environ first

    result = trigger_backfill(domains=domains)
    if result.get("triggered"):
        console.print(Panel.fit(f"[bold green]✓ Backfill triggered[/bold green]\n\n{result['note']}", border_style="green"))
    else:
        console.print(Panel(
            f"Didn't trigger backfill: {result.get('reason')}\n{result.get('html_url', '')}",
            title="!", border_style="yellow",
        ))


def _check_requirements() -> None:
    """Show a pass/warn checklist for the tools this wizard shells out to,
    and let the user bail out *before* spending time on DB setup + Garmin
    login if something's missing — rather than discovering it only once the
    wizard reaches the GitHub step."""
    console.print("[bold]Checking requirements...[/bold]\n")

    warnings: list[str] = []

    if shutil.which("git"):
        console.print("  [green]✓[/green] git   found")
    else:
        console.print(
            "  [yellow]![/yellow] git   not found — you'll need to type your GitHub "
            "repo (owner/repo) manually instead of it being auto-detected"
        )
        warnings.append("git is not installed")

    if not shutil.which("gh"):
        console.print(
            "  [red]✗[/red] gh    not installed — can't set GitHub secrets or "
            "trigger the backfill automatically (install: https://cli.github.com)"
        )
        warnings.append("gh CLI is not installed")
    else:
        try:
            auth = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, timeout=10)
        except Exception:
            auth = None
        if auth is not None and auth.returncode == 0:
            console.print("  [green]✓[/green] gh    found and authenticated")
        else:
            console.print("  [yellow]![/yellow] gh    found, but not authenticated — run `gh auth login`")
            warnings.append("gh is installed but not authenticated (run `gh auth login`)")

    console.print()

    if not warnings:
        console.print("[green]All requirements satisfied.[/green]\n")
        return

    console.print(Panel(
        "Missing:\n" + "\n".join(f"  • {w}" for w in warnings) + "\n\n"
        "You can still continue — the Neon/Garmin steps below work regardless. "
        "The GitHub Actions secrets + automatic backfill trigger step will be "
        "skipped (with manual instructions) until these are fixed.",
        title="!", border_style="yellow",
    ))
    if not questionary.confirm("Continue anyway?", default=True).ask():
        console.print("[yellow]Stopped — fix the above and re-run `uv run install.py`.[/yellow]")
        sys.exit(0)
    console.print()


def run_setup(domains_override: set[str] | None = None) -> None:
    console.print(Panel.fit(
        "[bold]garmin-data setup[/bold]\n\n"
        "This will: connect to your Neon database, log you in to Garmin, "
        "apply the schema, push your GitHub Actions secrets, and trigger "
        "the initial history backfill to run on GitHub's servers (not this "
        "machine).",
        border_style="cyan",
    ))

    _check_requirements()

    existing = _read_existing_env()

    database_url = _prompt_db_url("Neon connection string (DATABASE_URL)", "DATABASE_URL", existing)
    _check_connection(database_url, "DATABASE_URL")

    console.print("\n[bold]Applying base schema...[/bold]")
    apply_schema(database_url)

    console.print("\n[bold]Choose data domains[/bold]")
    preselected = {d.key for d in DOMAINS if d.default_enabled}
    selected_domains = domains_override if domains_override is not None else _prompt_domain_checklist(preselected)

    applied = apply_migrations(database_url, enabled_domains=selected_domains)
    console.print(f"[green]✓[/green] Schema applied ({len(applied)} migration(s)).")

    with psycopg.connect(database_url) as conn:
        set_enabled_domains(conn, selected_domains)

    console.print("\n[bold]Garmin login[/bold]")
    interactive_login()
    with psycopg.connect(database_url) as conn:
        save_token_to_db(conn)
    console.print("[green]✓[/green] Token saved to database.")

    env_updates = {"DATABASE_URL": database_url}

    console.print("\n[bold]GitHub Actions secrets + backfill trigger[/bold]")
    gh_ok, gh_message = _gh_available()
    if not gh_ok:
        console.print(f"[yellow]![/yellow] {gh_message}")
        _write_env(env_updates)
        _print_manual_secret_instructions(database_url)
        _print_manual_next_steps()
        return

    repo = _detect_github_repo()
    if not repo:
        repo = questionary.text("GitHub repo (owner/repo) this is pushed to:").ask()
    if not repo:
        console.print("[yellow]![/yellow] No repo detected — skipping secrets + backfill trigger.")
        _write_env(env_updates)
        _print_manual_secret_instructions(database_url)
        _print_manual_next_steps()
        return

    _set_gh_secret(repo, "NEON_DATABASE_URL", database_url)
    console.print("[green]✓[/green] Set NEON_DATABASE_URL secret.")

    ntfy_topic = questionary.text(
        "Optional: ntfy.sh topic for failure notifications (blank to skip):"
    ).ask() or ""
    if ntfy_topic.strip():
        _set_gh_secret(repo, "NTFY_TOPIC", ntfy_topic.strip())
        console.print("[green]✓[/green] Set NTFY_TOPIC secret.")

    github_token = questionary.password(
        "Fine-grained GitHub PAT (Actions: Read and write, scoped to this repo only) "
        "— needed so this wizard can trigger workflow runs:"
    ).ask()
    if github_token and github_token.strip():
        env_updates["GITHUB_TOKEN"] = github_token.strip()
        env_updates["GITHUB_REPO"] = repo
        _write_env(env_updates)

        start_date = questionary.text(
            "Backfill start date (YYYY-MM-DD, blank = 2 years ago):"
        ).ask() or ""

        from workflow_tools import check_backfill_status, trigger_backfill  # lazy: needs GITHUB_TOKEN/GITHUB_REPO in os.environ first

        result = trigger_backfill(start_date.strip() or None, domains=selected_domains)
        if result.get("triggered"):
            status = check_backfill_status()
            console.print(Panel.fit(
                f"[bold green]✓ Backfill triggered[/bold green]\n\n{result['note']}\n\n"
                f"Current status: {status.get('status', 'unknown')} — "
                f"{status.get('html_url', '(no run URL yet, check the Actions tab)')}",
                border_style="green",
            ))
        else:
            console.print(Panel(
                f"Didn't trigger backfill: {result.get('reason')}\n"
                f"{result.get('html_url', '')}",
                title="!", border_style="yellow",
            ))
    else:
        console.print("[yellow]![/yellow] No PAT provided — skipping automatic backfill trigger.")
        _write_env(env_updates)

    _print_manual_next_steps()


def _print_manual_next_steps() -> None:
    console.print(Panel.fit(
        "[bold]Setup complete.[/bold]\n\n"
        "- Check backfill progress in your repo's Actions tab (workflow: "
        "backfill.yml) — it can take a few hours; safe to leave running.\n"
        "- Password changed later? Run `uv run install.py --reauth`.",
        border_style="cyan",
    ))
    console.print(Panel.fit(
        "Want Claude to be able to query this data for you? See the separate "
        "[bold]postgres-mcp[/bold] project — point it at this same database "
        "using a read-only role you create there. Not something this wizard "
        "sets up.",
        title="Optional: querying with Claude", border_style="blue",
    ))


def run_reauth() -> None:
    console.print(Panel.fit("[bold]Garmin re-authentication[/bold]", border_style="cyan"))
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL") or _prompt_db_url(
        "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing
    )
    _check_connection(database_url, "DATABASE_URL")
    interactive_login()
    with psycopg.connect(database_url) as conn:
        save_token_to_db(conn)
    console.print("[green]✓[/green] Refreshed token saved to database. Future syncs will use it.")


def run_upgrade(domains_override: set[str] | None = None) -> None:
    console.print(Panel.fit(
        "[bold]garmin-data upgrade[/bold]\n\n"
        "Applies any pending schema migrations, lets you add (or remove) "
        "data domains, and triggers a targeted backfill for anything newly "
        "enabled that has history to pull.",
        border_style="cyan",
    ))
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL") or _prompt_db_url(
        "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing
    )
    _check_connection(database_url, "DATABASE_URL")

    console.print("\n[bold]Checking for a pre-migrations database...[/bold]")
    apply_schema(database_url)  # creates tracker tables + adopts an existing DB if needed

    with psycopg.connect(database_url) as conn:
        current_enabled = get_enabled_domains(conn)

    newly_available = [d for d in DOMAINS if d.key not in current_enabled]
    pending_names = _pending_migrations(database_url, current_enabled)
    _print_whats_new(newly_available, pending_names)

    selected_domains = domains_override if domains_override is not None else _prompt_domain_checklist(current_enabled)

    applied = apply_migrations(database_url, enabled_domains=selected_domains)

    with psycopg.connect(database_url) as conn:
        set_enabled_domains(conn, selected_domains)

    with psycopg.connect(database_url) as conn:
        coach_cfg = get_coach_loop(conn)
        if coach_cfg:
            apply_coach_grants(conn, CoachLoopRoles.from_config(coach_cfg))
            conn.commit()
            console.print("[green]✓[/green] Re-applied coach-loop grants (covers tables added by this upgrade).")

    if applied:
        console.print(f"[green]✓[/green] Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        console.print("[green]✓[/green] Nothing pending — already up to date.")

    newly_enabled = selected_domains - current_enabled
    backfillable = {k for k in newly_enabled if DOMAINS_BY_KEY[k].backfill is not None}

    if not newly_enabled:
        console.print("No new domains enabled.")
    elif not backfillable:
        console.print(
            f"[green]✓[/green] Enabled {', '.join(sorted(newly_enabled))} — "
            "snapshot-only, no backfill needed. The next scheduled sync will populate them."
        )
    else:
        _trigger_targeted_backfill(existing, backfillable)

    _print_manual_next_steps()


COACH_LOOP_CONSENT = (
    "[bold]Coach loop — optional, off by default.[/bold]\n\n"
    "Turning this on [bold]grants a component write access to your Garmin account[/bold] "
    "(the enactor — seed_workouts.py / sync_schedule.py — can upload workouts to your "
    "library and put them on your calendar) and [bold]creates two scoped database users[/bold]:\n\n"
    "  • coach — for the MCP write tool. Can edit training_plan and protocols. "
    "Cannot read your Garmin token and cannot approve anything.\n"
    "  • scheduler — for the enactor. Can read your Garmin token and write the workout "
    "ledger. Acts only on plan rows you approve with plan_admin.py.\n\n"
    "Nothing is written to Garmin until you approve a plan row AND run the enactor by "
    "hand. A read-only install never needs this."
)


def run_coach_loop_optin(database_url: str, existing: dict[str, str]) -> None:
    console.print(Panel(COACH_LOOP_CONSENT, title="Coach loop", border_style="magenta"))
    if not questionary.confirm("Enable the coach loop?", default=False).ask():
        console.print("Coach loop not enabled.")
        return

    with psycopg.connect(database_url) as conn:
        enabled = get_enabled_domains(conn)
        previous = get_coach_loop(conn) or {}
    if "workouts" not in enabled:
        console.print("The coach loop needs the read-only [bold]workouts[/bold] domain — enabling it.")
        enabled = enabled | {"workouts"}
        apply_migrations(database_url, enabled_domains=enabled)
        with psycopg.connect(database_url) as conn:
            set_enabled_domains(conn, enabled)

    read_role = questionary.text(
        "Your MCP read-only DB role, so it can SELECT the new tables (blank to skip):",
        default=previous.get("read_role") or "",
    ).ask() or ""
    roles = CoachLoopRoles(read=read_role.strip() or None)

    env_updates: dict[str, str] = {}
    with psycopg.connect(database_url) as conn:
        for role, env_key in ((roles.coach, "COACH_DATABASE_URL"), (roles.scheduler, "SCHEDULER_DATABASE_URL")):
            if role_exists(conn, role) and role_of_url(existing.get(env_key)) == role:
                continue  # already set up and we hold its URL — don't rotate the password
            password = generate_password()
            ensure_role(conn, role, password)
            env_updates[env_key] = url_for_role(database_url, role, password)
        apply_coach_grants(conn, roles)
        inserted = seed_default_protocols(conn)
        conn.commit()
        for role in (roles.coach, roles.scheduler):
            assert_role_isolated(conn, role)
        set_coach_loop(conn, roles.to_config())

    console.print(f"[green]✓[/green] Roles + grants applied; {inserted} default protocol(s) seeded.")
    if env_updates:
        _write_env(env_updates)
        console.print(Panel(
            "Written to .env:\n  " + "\n  ".join(env_updates) + "\n\n"
            "• COACH_DATABASE_URL → set it on your MCP server (run_write_sql).\n"
            "• SCHEDULER_DATABASE_URL → used by seed_workouts.py / sync_schedule.py.\n"
            "• Approvals use your existing DATABASE_URL: uv run plan_admin.py list",
            title="Coach loop credentials", border_style="green",
        ))

    scheduler_url = env_updates.get("SCHEDULER_DATABASE_URL") or existing.get("SCHEDULER_DATABASE_URL")
    repo = _detect_github_repo()
    if scheduler_url and repo and _gh_available()[0] and questionary.confirm(
        "Also store SCHEDULER_DATABASE_URL as a GitHub Actions secret so the manual "
        "garmin-enact workflow can run? This places a standing Garmin-write-capable "
        "credential in CI (the local commands don't need it).",
        default=False,
    ).ask():
        _set_gh_secret(repo, "SCHEDULER_DATABASE_URL", scheduler_url)
        console.print("[green]✓[/green] Set SCHEDULER_DATABASE_URL secret.")


def _offer_coach_loop() -> None:
    existing = _read_existing_env()
    database_url = existing.get("DATABASE_URL")
    if not database_url:
        return
    with psycopg.connect(database_url) as conn:
        if get_coach_loop(conn):
            return
    run_coach_loop_optin(database_url, existing)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reauth", action="store_true", help="Re-authenticate to Garmin only, skip full setup.")
    parser.add_argument(
        "--upgrade", action="store_true",
        help="Adopt/upgrade an existing install: apply pending migrations, enable new domains, trigger a targeted backfill.",
    )
    parser.add_argument(
        "--domains", type=str, default=None,
        help="Comma-separated domain keys (see domains.py), bypasses the interactive checklist (setup and --upgrade). Also settable via GARMIN_DOMAINS.",
    )
    parser.add_argument(
        "--coach-loop", action="store_true",
        help="Opt in to the coach loop: creates the coach/scheduler DB roles and grants a component Garmin write access.",
    )
    args = parser.parse_args()

    raw_domains = args.domains or os.environ.get("GARMIN_DOMAINS")
    domains_override = _parse_domains_arg(raw_domains) if raw_domains else None

    try:
        if args.reauth:
            run_reauth()
        elif args.coach_loop:
            existing = _read_existing_env()
            database_url = existing.get("DATABASE_URL") or _prompt_db_url(
                "Neon connection string (DATABASE_URL)", "DATABASE_URL", existing)
            _check_connection(database_url, "DATABASE_URL")
            run_coach_loop_optin(database_url, existing)
        elif args.upgrade:
            run_upgrade(domains_override=domains_override)
            _offer_coach_loop()
        else:
            run_setup(domains_override=domains_override)
            _offer_coach_loop()
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/yellow]")
        sys.exit(1)


if __name__ == "__main__":
    main()
