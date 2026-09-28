"""
Inspo — three simple screens over the engine:
  1. Type competitor names
  2. Review the channels found for each (edit, add links), then approve
  3. Read the digest

Everything the pipeline does is still recorded in the Google Sheet, which is
for QA; this app is only for input and a clean result.
"""

import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import engine
from adapters.feed_discovery import parse_channel_lines, sources_for_competitor, newsletter_needing_subscription
from adapters.newsletter_signup import submit_signup
from pipeline import newsletter_routing
from adapters.scraper_registry import has_scraper, platform_of, pretty
from pipeline.channel_discovery import discover_channels
from pipeline import media
from pipeline.suggestions import suggest_new_voices

load_dotenv()

# Streamlit Cloud secrets -> the env vars the engine reads (only if a secrets file exists).
if any(p.exists() for p in (Path.home() / ".streamlit" / "secrets.toml", Path(__file__).parent / ".streamlit" / "secrets.toml")):
    for key in ("ANTHROPIC_API_KEY", "APIFY_TOKEN", "GOOGLE_SHEET_ID",
                "GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON_CONTENT"):
        if key not in os.environ and key in st.secrets:
            os.environ[key] = st.secrets[key]

st.set_page_config(page_title="Inspo", layout="centered")

CLIENT_ID, STREAM = "ksenia", "competitor_monitoring"
DEFAULT_SIGNUP_EMAIL = "anastasia.sidko@gmail.com"


@st.cache_resource
def get_storage():
    from storage.sheets_backend import SheetsBackend
    storage = SheetsBackend.from_env()
    storage.init()
    return storage


state = st.session_state
state.setdefault("step", 1)

sheet_id = os.environ.get("GOOGLE_SHEET_ID", "")
head_left, head_right = st.columns([3, 1])
head_left.title("Inspo")
if sheet_id:
    head_right.link_button("Open Google Sheet", f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
st.text_input("Email to use for newsletter signups", value=DEFAULT_SIGNUP_EMAIL, key="signup_email")


# ---------------------------------------------------------------- step 1
if state.step == 1:
    st.subheader("1. Who do you want to follow?")
    st.info("Only the names you type here are collected. Other competitors saved in the Settings tab "
            "of the Google Sheet are not run.")
    names_text = st.text_area("Competitor names, one per line", height=150, value=state.pop("prefill_name", ""))
    days = st.number_input("Look back (days)", min_value=1, max_value=90, value=7)
    refresh = st.checkbox("Search again even if channels were saved before")

    if st.button("Find their channels", type="primary"):
        names = [n.strip() for n in names_text.splitlines() if n.strip()]
        if not names:
            st.warning("Type at least one name.")
        else:
            saved = {r.get("competitor"): r.get("channels", "")
                     for r in get_storage().get_saved_items(CLIENT_ID, STREAM, active_only=False)}
            channels, sources = {}, {}
            with st.spinner("Searching the web for their channels…"):
                for name in names:
                    if saved.get(name) and not refresh:
                        channels[name] = saved[name]
                    else:
                        found = discover_channels(name)
                        channels[name] = "\n".join(f"{c['label']}: {c['url']}" for c in found)
                        sources[name] = [f"{c['label']}: {c['source']}" for c in found]
            state.channels, state.sources, state.names, state.lookback, state.step = channels, sources, names, int(days), 2
            st.rerun()


# ---------------------------------------------------------------- step 2
elif state.step == 2:
    st.subheader("2. Check the channels")
    st.caption("One channel per line, as  Label: link.  Edit, delete, or add any links you know. "
               "When it looks right, approve. Only the people below will be collected.")
    edited = {}
    left_col, right_col = st.columns([3, 2])
    with right_col:
        st.markdown("**Comments for this run**")
        run_comment_text = st.text_area(
            "Comments for this run", height=280, label_visibility="collapsed", key="run_comment",
            placeholder="Only for this run, nothing is saved. They win over the general rules.\n\n"
                        "For example:\nFor Instagram, look back only 7 days.\nIgnore posts about hiring this time.")
        st.caption("The look-back you set on the first screen applies to every channel unless you say otherwise here. "
                   "The Rules tab is not changed.")
    with left_col:
        for name in state.names:
            edited[name] = st.text_area(name, value=state.channels[name], height=280, key=f"ch_{name}")
            if not edited[name].strip():
                st.caption("Nothing found. Add links above, or this competitor will be skipped.")
            missing = sorted({platform_of(u) for _, u in parse_channel_lines(edited[name])
                              if platform_of(u) and not has_scraper(platform_of(u))})
            if missing:
                st.info("No scraper set up yet for " + ", ".join(pretty(p) for p in missing) +
                        ". When you approve, I'll search Apify for one, use it (spend capped at $0.10 per run) "
                        "and tell you which one.")
            if state.sources.get(name):
                with st.expander("Where these links came from"):
                    for line in state.sources[name]:
                        st.write(line)

            nl = newsletter_needing_subscription(parse_channel_lines(edited[name]))
            if nl:
                nl_label, nl_url = nl
                st.caption(f"{name} has a newsletter with no public feed, so it can only be collected by subscribing.")
                if st.button(f"Subscribe to {name}'s newsletter", key=f"sub_{name}"):
                    with st.spinner("Submitting the signup form…"):
                        result = submit_signup(nl_url, state.get("signup_email", DEFAULT_SIGNUP_EMAIL))
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
        if st.button(f"Check on {pending} pending newsletter folder(s)"):
            with st.spinner("Checking for new mail from subscribed newsletters…"):
                result = newsletter_routing.try_finish_pending()
            if result["not_configured"]:
                st.warning("The app's own Gmail login isn't set up yet, so this can't be checked "
                           "automatically. See the setup steps in the README.")
            elif result["routed"]:
                st.success(f"Folder set up for: {', '.join(result['routed'])}.")
                if result["still_pending"]:
                    st.caption(f"{result['still_pending']} still waiting on their first email.")
            else:
                st.info(f"Still waiting — {result['still_pending']} newsletter(s) haven't sent anything yet.")

    left, right = st.columns([1, 3])
    if left.button("Back"):
        state.step = 1
        st.rerun()
    if right.button("Approve and run", type="primary"):
        sources, skipped = [], []
        with st.spinner("Checking the links, collecting and writing the digest. This can take a few minutes…"):
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
                st.error("None of these links can be collected yet. Add a website, blog, podcast, YouTube or LinkedIn link.")
            else:
                try:
                    result = engine.run_stream(
                        stream=STREAM, inputs=inputs, mode="typed_only", lookback_days=state.lookback,
                        started_by="app", client_id=CLIENT_ID, storage=get_storage(),
                        run_comment_text=run_comment_text)
                    state.comment_info = result.get("comment_summary", []), result.get("comment_warnings", [])
                    state.digest, state.skipped, state.notices, state.step = result["digest"], skipped, result.get("notices", []), 3
                    state.map_text, state.videos = result.get("competitor_map", ""), result.get("video_candidates", [])
                    state.video_done, state.suggestions = {}, None
                except Exception as e:
                    st.error(f"The run failed: {e}")
        if state.step == 3:
            st.rerun()


# ---------------------------------------------------------------- step 3
else:
    st.subheader("3. Your digest")
    for note in state.get("notices", []):
        st.warning(note)
    applied, comment_warnings = state.get("comment_info", ([], []))
    if applied:
        st.info("Your comments for this run were applied (this run only, nothing saved):\n\n" +
                "\n".join(f"- {line}" for line in applied))
    for w in comment_warnings:
        st.warning(w)
    st.markdown(state.digest.replace("\n", "  \n"))

    if state.get("map_text"):
        st.subheader("Posting activity")
        st.text(state.map_text)

    if state.get("videos"):
        st.subheader("Videos worth a look")
        st.caption("These posts were relevant and have a video attached. Nothing is fetched unless you approve it; "
                   "each fetch is a paid step, capped at a few cents.")
        for i, v in enumerate(state.videos):
            st.write(f"{v['author']}'s post has a {v['length']} video. [Open the post]({v['post_url']})")
            done = state.video_done.get(i)
            if done:
                (st.success if done["ok"] else st.warning)(done["message"])
            elif st.button("Fetch this video and add it to the digest", key=f"vid_{i}"):
                with st.spinner("Finding a transcript scraper and fetching the video…"):
                    got = media.get_transcript(v)
                    if got["text"]:
                        try:
                            res = engine.run_video(v, got["text"], STREAM, state.lookback, "app",
                                                   client_id=CLIENT_ID, storage=get_storage())
                            state.digest = res["digest"]
                            state.video_done[i] = {"ok": True, "message": f"Added. The digest above now includes "
                                                   f"the video (fetched with {got['actor']}, ${got['cost_usd']:.3f})."}
                        except Exception as e:
                            state.video_done[i] = {"ok": False, "message": f"The video text was fetched but the analysis failed: {e}"}
                    else:
                        state.video_done[i] = {"ok": False, "message": got["notice"] or "Nothing could be fetched."}
                st.rerun()

    st.subheader("People you might add")
    if state.get("suggestions") is None:
        st.caption("Searches the web for 5 more people who write about the same topics as this digest. This uses the "
                   "Anthropic search, so it only runs when you ask.")
        if st.button("Suggest people to follow"):
            with st.spinner("Searching the web…"):
                followed = [r.get("competitor") for r in get_storage().get_saved_items(CLIENT_ID, STREAM, active_only=False)]
                state.suggestions = suggest_new_voices(state.digest, [n for n in followed + state.names if n])
            st.rerun()
    elif not state.suggestions:
        st.info("No suggestions came back with a source I could check. Try again later.")
    else:
        for i, p in enumerate(state.suggestions):
            st.markdown(f"**{p['name']}** — {p['why']}  \n[Where I found them]({p['url']})")
            st.caption(f"Reach: {p['reach']} ([source]({p['reach_source']}))" if p["reach"]
                       else "Reach: not found in the search results")
            if st.button(f"Add {p['name']} to competitors", key=f"add_{i}"):
                state.prefill_name, state.step = p["name"], 1
                st.rerun()

    if state.skipped:
        with st.expander("Links that were not collected"):
            for line in state.skipped:
                st.write(line)
    if st.button("Start over"):
        state.step = 1
        st.rerun()
