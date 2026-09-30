"""
Trend research — same shape as the Competitors screens, with one difference:
a headline-approval gate between scraping and analysis. Nothing is scored or
written to a digest until you've approved which headlines are worth reading.

Screens (state.trends_step):
  1. What do you want to track? — journal/newsletter names, one per line.
     No AI channel discovery here (unlike Competitors): you already know these
     sources, so step 2 is where you paste their links directly.
  2. Add links for each — paste  Label: url  lines per name (RSS/site/newsletter),
     same newsletter-subscribe button Competitors has, then scan for headlines
     (Fetch + Dedup only — no AI cost yet).
  3. Review headlines — Approve / Disapprove each scraped headline.
  4. Your digest — Extract + Score + Synthesize ran on just the approved items.

Called from app.py as  Trends.trend_app.render(get_storage, signup_email).
"""

from pathlib import Path

import streamlit as st

import engine
from config import load_local_config
from adapters.feed_discovery import parse_channel_lines, sources_for_competitor, newsletter_needing_subscription
from adapters.newsletter_signup import submit_signup
from adapters.scraper_registry import has_scraper, platform_of, pretty
from pipeline import newsletter_routing, usage_log

CLIENT_ID = "ksenia"
STREAM = "trend_research"
CONFIG_PATH = str(Path(__file__).parent / "config_trend_research.yaml")

# The one tab this stream needs beyond the base Sheets schema — passed as
# extra_tabs= to SheetsBackend for the Trends spreadsheet only (see app.py's
# get_storage_trends() and create_trends_sheet.py, both of which import this
# so the schema is defined in exactly one place).
HEADLINES_TAB = {
    "Headlines": ["run_id", "source", "title", "url", "published_at", "status", "decided_at"],
}


def _load_config() -> dict:
    return load_local_config(STREAM, config_path=CONFIG_PATH)


def render(get_storage, signup_email: str) -> None:
    state = st.session_state
    state.setdefault("trends_step", 1)

    if state.trends_step == 1:
        _step1(state, get_storage)
    elif state.trends_step == 2:
        _step2(state, get_storage, signup_email)
    elif state.trends_step == 3:
        _step3(state, get_storage)
    else:
        _step4(state, get_storage)


# ---------------------------------------------------------------- step 1
def _step1(state, get_storage) -> None:
    st.subheader("1. What do you want to track?")
    st.info("Journals, newsletters, and other resources — one per line. Next you'll paste each one's "
            "RSS/website/newsletter link directly (no web search, since you already know these sources).")
    names_text = st.text_area("Journals & other resources, one per line", height=150,
                              value=state.pop("trends_prefill_name", ""), key="trends_names_text")
    days = st.number_input("Look back (days)", min_value=1, max_value=90, value=7, key="trends_days")

    if st.button("Continue", type="primary", key="trends_step1_continue"):
        names = [n.strip() for n in names_text.splitlines() if n.strip()]
        if not names:
            st.warning("Type at least one journal or resource name.")
        else:
            saved = {r.get("source"): r.get("channels", "")
                     for r in get_storage().get_saved_items(CLIENT_ID, STREAM, active_only=False)}
            state.trends_channels = {name: saved.get(name, "") for name in names}
            state.trends_names, state.trends_lookback, state.trends_step = names, int(days), 2
            st.rerun()


# ---------------------------------------------------------------- step 2
def _step2(state, get_storage, signup_email: str) -> None:
    st.subheader("2. Add links for each")
    st.caption("One link per line, as  Label: link  — the RSS feed, website, or newsletter signup page. "
                "Previously saved links (if any) are pre-filled below; edit, delete, or add more.")
    edited = {}
    for name in state.trends_names:
        edited[name] = st.text_area(name, value=state.trends_channels.get(name, ""), height=120,
                                    key=f"trends_ch_{name}")
        if not edited[name].strip():
            st.caption("No links yet — add at least one above, or this source will be skipped.")
        missing = sorted({platform_of(u) for _, u in parse_channel_lines(edited[name])
                          if platform_of(u) and not has_scraper(platform_of(u))})
        if missing:
            st.info("No scraper set up yet for " + ", ".join(pretty(p) for p in missing) +
                    ". When you continue, I'll search Apify for one, use it (spend capped at $0.10 per run) "
                    "and tell you which one.")

        nl = newsletter_needing_subscription(parse_channel_lines(edited[name]))
        if nl:
            nl_label, nl_url = nl
            st.caption(f"{name} has a newsletter with no public feed, so it can only be collected by subscribing.")
            if st.button(f"Subscribe to {name}'s newsletter", key=f"trends_sub_{name}"):
                with st.spinner("Submitting the signup form…"):
                    result = submit_signup(nl_url, signup_email)
                status = result["status"]
                (st.success if status == "subscribed" else st.info if status == "pending_confirmation"
                 else st.warning)(result["message"])
                if status in ("subscribed", "pending_confirmation"):
                    newsletter_routing.record_subscription(name, nl_url)
                    st.caption(f"Once {name}'s first newsletter email arrives, it'll be moved into a "
                               f"'{newsletter_routing.LABEL_NAME}' Gmail folder automatically — check "
                               f"back with the button below.")
                if status == "pending_confirmation":
                    st.caption("Also: this newsletter needs a confirmation click, which isn't automated yet "
                               "(needs the app's own Gmail login) — please click the confirmation link yourself.")

    pending = newsletter_routing.pending_count()
    if pending:
        if st.button(f"Check on {pending} pending newsletter folder(s)", key="trends_check_pending"):
            with st.spinner("Checking for new mail from subscribed newsletters…"):
                result = newsletter_routing.try_finish_pending()
            if result["not_configured"]:
                st.warning("The app's own Gmail login isn't set up yet — see the setup steps in the README.")
            elif result["routed"]:
                st.success(f"Folder set up for: {', '.join(result['routed'])}.")
            else:
                st.info(f"Still waiting — {result['still_pending']} newsletter(s) haven't sent anything yet.")

    left, right = st.columns([1, 3])
    if left.button("Back", key="trends_step2_back"):
        state.trends_step = 1
        st.rerun()
    if right.button("Scan headlines", type="primary", key="trends_step2_scan"):
        sources, skipped = [], []
        with st.spinner("Checking the links and scraping headlines (no AI cost yet)…"):
            for name, text in edited.items():
                channels = parse_channel_lines(text)
                if not channels:
                    continue
                get_storage().upsert_competitor(CLIENT_ID, STREAM, name, text.strip())
                s, sk = sources_for_competitor(name, channels)
                sources += s
                skipped += sk
            inputs = [f"{s['name']} | {s['feed_url']}" for s in sources]
            if not inputs:
                st.error("None of these links can be collected yet. Add an RSS, website, or newsletter link.")
            else:
                try:
                    result = engine.run_headline_scan(
                        stream=STREAM, inputs=inputs, mode="typed_only", lookback_days=state.trends_lookback,
                        started_by="app", client_id=CLIENT_ID, storage=get_storage(), config=_load_config())
                    state.trends_run_id = result["run_id"]
                    state.trends_headlines = result["headlines"]
                    state.trends_notices = result.get("notices", [])
                    state.trends_skipped = skipped
                    state.trends_decisions = {}
                    state.trends_step = 3
                except Exception as e:
                    st.error(f"The scan failed: {e}")
        if state.trends_step == 3:
            st.rerun()


# ---------------------------------------------------------------- step 3
def _step3(state, get_storage) -> None:
    st.subheader("3. Review headlines")
    for note in state.get("trends_notices", []):
        st.warning(note)

    headlines = state.get("trends_headlines", [])
    if not headlines:
        st.info("Nothing new since last time — every source came back empty or everything was already seen.")
    else:
        st.caption(f"{len(headlines)} new headline(s). Approve the ones worth reading in full — only those "
                   "get analyzed and go into the digest.")
        for i, item in enumerate(headlines):
            url = item.get("source_url", "")
            decision = state.trends_decisions.get(url)
            with st.container(border=True):
                st.markdown(f"**{item.get('title') or '(untitled)'}**")
                st.caption(f"{item.get('source_name', '')}" + (f" · {item.get('published_date')}"
                          if item.get("published_date") else ""))
                if url:
                    st.markdown(f"[Open link]({url})")
                if decision:
                    st.success("Approved" if decision == "approved" else "Disapproved")
                else:
                    c1, c2 = st.columns(2)
                    if c1.button("Approve", key=f"trends_approve_{i}", type="primary"):
                        state.trends_decisions[url] = "approved"
                        get_storage().update_headline_status(state.trends_run_id, url, "approved")
                        st.rerun()
                    if c2.button("Disapprove", key=f"trends_reject_{i}"):
                        state.trends_decisions[url] = "rejected"
                        get_storage().update_headline_status(state.trends_run_id, url, "rejected")
                        st.rerun()

    left, right = st.columns([1, 3])
    if left.button("Back", key="trends_step3_back"):
        state.trends_step = 2
        st.rerun()
    if right.button("Get my digest", type="primary", key="trends_step3_digest"):
        approved = [item for item in headlines if state.trends_decisions.get(item.get("source_url")) == "approved"]
        undecided = sum(1 for item in headlines if item.get("source_url") not in state.trends_decisions)
        if not approved:
            st.warning("Approve at least one headline first.")
        else:
            if undecided:
                st.caption(f"Proceeding with {len(approved)} approved item(s); {undecided} left undecided are excluded.")
            with st.spinner("Analyzing the approved headlines and writing the digest…"):
                try:
                    result = engine.run_digest_from_headlines(
                        run_id=state.trends_run_id, approved_items=approved, stream=STREAM,
                        lookback_days=state.trends_lookback, started_by="app", client_id=CLIENT_ID,
                        storage=get_storage(), config=_load_config())
                    usage_log.flush(get_storage())
                    state.trends_digest, state.trends_stats, state.trends_step = result["digest"], result["stats"], 4
                except Exception as e:
                    st.error(f"The digest failed: {e}")
        if state.trends_step == 4:
            st.rerun()


# ---------------------------------------------------------------- step 4
def _step4(state, get_storage) -> None:
    st.subheader("4. Your digest")
    st.markdown(state.trends_digest.replace("\n", "  \n"))

    if state.get("trends_skipped"):
        with st.expander("Links that were not collected"):
            for line in state.trends_skipped:
                st.write(line)

    if st.button("Start over", key="trends_start_over"):
        for key in list(state.keys()):
            if key.startswith("trends_"):
                del state[key]
        state.trends_step = 1
        st.rerun()
