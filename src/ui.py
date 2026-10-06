from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Any

import streamlit as st


def inject_theme() -> None:
    st.markdown(
        r"""
<style>
:root {
  --canvas:#F3F6FA;
  --surface:#FFFFFF;
  --surface-soft:#F8FAFC;
  --ink:#17212B;
  --ink-2:#344054;
  --muted:#667085;
  --muted-2:#98A2B3;
  --line:#D9E1EA;
  --line-soft:#E9EEF4;
  --teal:#008C85;
  --teal-dark:#006F6A;
  --teal-soft:#E8F8F6;
  --cyan:#0E9CCB;
  --cyan-soft:#EAF7FC;
  --violet:#7357D9;
  --violet-soft:#F1EEFF;
  --amber:#E79A15;
  --amber-soft:#FFF6E5;
  --coral:#E85C4A;
  --coral-soft:#FFF0ED;
  --green:#159A62;
  --green-soft:#EAF8F1;
  --shadow-sm:0 1px 2px rgba(16,24,40,.04);
  --shadow-md:0 8px 24px rgba(26,39,52,.07);
  --shadow-lg:0 18px 48px rgba(26,39,52,.10);
}

html, body, [data-testid="stAppViewContainer"] {
  background:
    radial-gradient(circle at 82% 0%, rgba(14,156,203,.06), transparent 28%),
    radial-gradient(circle at 16% 20%, rgba(115,87,217,.045), transparent 27%),
    var(--canvas);
}
[data-testid="stAppViewContainer"] > .main { background:transparent; }
[data-testid="stHeader"] { background:rgba(243,246,250,.90); backdrop-filter:blur(10px); }

#MainMenu, footer { visibility:hidden; }

/* Keep sidebar reopen button accessible */
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    visibility: visible !important;
    display: flex !important;
    opacity: 1 !important;
    pointer-events: auto !important;
    z-index: 999999 !important;
}
.block-container { max-width:1380px; padding-top:1.35rem; padding-bottom:4rem; }

/* Dark graphite sidebar — visually separate from the working canvas */
[data-testid="stSidebar"] {
  background:linear-gradient(180deg,#17212B 0%,#1C2935 58%,#13202B 100%);
  border-right:0;
  box-shadow:8px 0 30px rgba(17,31,44,.12);
}
[data-testid="stSidebar"] > div:first-child { padding-top:1.35rem; }
[data-testid="stSidebar"] * { color:#EAF0F6; }
[data-testid="stSidebar"] hr { border-color:rgba(255,255,255,.12); }
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] .stCaption,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"] { color:#B6C1CD !important; }
[data-testid="stSidebar"] div[data-baseweb="select"] > div,
[data-testid="stSidebar"] [data-testid="stExpander"] {
  background:rgba(255,255,255,.06) !important;
  border-color:rgba(255,255,255,.14) !important;
}
[data-testid="stSidebar"] div[data-baseweb="select"] * { color:#F8FAFC !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary,
[data-testid="stSidebar"] [data-testid="stExpander"] summary * { color:#F8FAFC !important; }
[data-testid="stSidebar"] div[data-testid="stButton"] > button:not([kind="primary"]),
[data-testid="stSidebar"] div[data-testid="stDownloadButton"] > button {
  background:rgba(255,255,255,.07);
  color:#F8FAFC !important;
  border:1px solid rgba(255,255,255,.16);
}
[data-testid="stSidebar"] div[data-testid="stButton"] > button:not([kind="primary"]):hover,
[data-testid="stSidebar"] div[data-testid="stDownloadButton"] > button:hover {
  background:rgba(255,255,255,.12);
  border-color:rgba(255,255,255,.28);
}
.sidebar-kicker { color:#68DDD2; font-size:.64rem; font-weight:850; letter-spacing:.12em; text-transform:uppercase; margin-bottom:7px; }
.sidebar-brand { color:#FFFFFF; font-size:1.18rem; font-weight:900; letter-spacing:-.028em; }
.sidebar-subtitle { color:#AEB9C5; font-size:.76rem; margin:4px 0 16px; }
.connection-ok {
  display:inline-flex; align-items:center; gap:8px; color:#BDF4DC !important;
  font-size:.76rem; font-weight:800; padding:8px 10px;
  background:rgba(21,154,98,.14); border:1px solid rgba(83,213,153,.28); border-radius:10px;
}
.connection-ok:before { content:""; width:8px; height:8px; border-radius:50%; background:#4FE0A2; box-shadow:0 0 0 4px rgba(79,224,162,.10); }

/* Rich header */
.app-hero {
  position:relative; overflow:hidden;
  display:grid; grid-template-columns:minmax(0,1.6fr) minmax(310px,.8fr); gap:28px; align-items:center;
  background:linear-gradient(120deg,#17212B 0%,#20313C 52%,#006F6A 118%);
  color:#FFFFFF; border-radius:20px; padding:30px 34px; margin:0 0 22px;
  box-shadow:var(--shadow-lg);
}
.app-hero:before {
  content:""; position:absolute; width:380px; height:380px; border-radius:50%; right:-145px; top:-235px;
  background:radial-gradient(circle,rgba(70,220,205,.30) 0%,rgba(70,220,205,0) 69%);
}
.app-hero:after {
  content:""; position:absolute; width:270px; height:270px; border-radius:50%; left:48%; bottom:-245px;
  background:radial-gradient(circle,rgba(121,103,228,.24) 0%,rgba(121,103,228,0) 70%);
}
.hero-left,.hero-right { position:relative; z-index:1; }
.hero-kicker { color:#74E7DC; font-size:.69rem; font-weight:900; letter-spacing:.13em; text-transform:uppercase; margin-bottom:7px; }
.hero-title { color:#FFFFFF; font-size:2.18rem; line-height:1.05; font-weight:950; letter-spacing:-.045em; margin:0; }
.hero-subtitle { color:#D5DEE8; font-size:.95rem; line-height:1.58; margin-top:11px; max-width:760px; }
.hero-badges { display:flex; flex-wrap:wrap; gap:8px; margin-top:17px; }
.hero-badge {
  display:inline-flex; align-items:center; gap:7px; padding:7px 10px; border-radius:999px;
  color:#EAF7F6; font-size:.72rem; font-weight:750; background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.13); backdrop-filter:blur(4px);
}
.hero-badge-dot { width:6px; height:6px; border-radius:50%; background:#63E6D8; }
.hero-right { display:grid; grid-template-columns:1fr 1fr; gap:10px; padding:4px; }
.hero-mini {
  min-height:84px; padding:13px 14px; border-radius:14px;
  background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.13); backdrop-filter:blur(5px);
}
.hero-mini-value { color:#FFFFFF; font-size:1.22rem; font-weight:900; letter-spacing:-.03em; }
.hero-mini-label { color:#BFCBD6; font-size:.68rem; line-height:1.3; margin-top:4px; }

/* Phase heading */
.phase-header { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; padding:5px 1px 13px; margin-top:3px; }
.phase-kicker { font-size:.67rem; font-weight:900; letter-spacing:.11em; text-transform:uppercase; margin-bottom:4px; }
.phase-title { color:var(--ink); font-size:1.5rem; font-weight:950; letter-spacing:-.035em; margin:0; }
.phase-subtitle { color:var(--muted); font-size:.82rem; line-height:1.48; margin-top:5px; max-width:790px; }
.phase-chip { display:inline-flex; align-items:center; gap:7px; border-radius:999px; padding:7px 10px; font-size:.72rem; font-weight:800; white-space:nowrap; border:1px solid var(--line); background:#FFFFFF; color:var(--ink-2); }
.phase-teal .phase-kicker{color:var(--teal)} .phase-cyan .phase-kicker{color:var(--cyan)}
.phase-violet .phase-kicker{color:var(--violet)} .phase-amber .phase-kicker{color:#B66D00}
.phase-coral .phase-kicker{color:var(--coral)}

/* Run summary banner */
.run-banner {
  display:grid; grid-template-columns:minmax(250px,1.7fr) repeat(3,minmax(150px,.7fr)); overflow:hidden;
  margin:0 0 16px; background:#FFFFFF; border:1px solid var(--line); border-radius:16px; box-shadow:var(--shadow-sm);
}
.run-cell { padding:15px 18px; border-right:1px solid var(--line-soft); min-width:0; }
.run-cell:last-child{border-right:none}
.run-label { color:var(--muted); font-size:.62rem; text-transform:uppercase; font-weight:900; letter-spacing:.08em; }
.run-value { color:var(--ink); font-size:.88rem; font-weight:850; margin-top:4px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.run-main .run-value { font-size:1rem; color:var(--teal-dark); }

/* Empty state fills space intentionally */
.empty-shell { display:grid; grid-template-columns:minmax(0,1.05fr) minmax(360px,.95fr); gap:24px; align-items:stretch; margin-top:8px; }
.empty-card { background:#FFFFFF; border:1px solid var(--line); border-radius:18px; padding:30px; box-shadow:var(--shadow-md); }
.empty-kicker { color:var(--teal); font-size:.68rem; font-weight:900; letter-spacing:.12em; text-transform:uppercase; }
.empty-title { color:var(--ink); font-size:1.72rem; font-weight:950; letter-spacing:-.04em; margin-top:8px; }
.empty-copy { color:var(--muted); font-size:.9rem; line-height:1.6; margin:10px 0 18px; max-width:620px; }
.empty-feature-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:15px; }
.empty-feature { padding:13px 14px; border:1px solid var(--line-soft); border-radius:12px; background:var(--surface-soft); }
.empty-feature strong { display:block; color:var(--ink); font-size:.77rem; }
.empty-feature span { display:block; color:var(--muted); font-size:.7rem; margin-top:3px; line-height:1.35; }
.migration-visual {
  min-height:330px; display:flex; flex-direction:column; justify-content:center;
  background:linear-gradient(145deg,#17212B 0%,#203540 56%,#006F6A 120%);
  border-radius:18px; padding:28px; box-shadow:var(--shadow-md); position:relative; overflow:hidden;
}
.migration-visual:after { content:""; position:absolute; width:250px; height:250px; border-radius:50%; right:-120px; top:-100px; background:rgba(83,221,206,.12); }
.path-row { display:grid; grid-template-columns:1fr 52px 1fr; align-items:center; gap:8px; position:relative; z-index:1; }
.path-node { padding:17px; border-radius:14px; background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.13); }
.path-node small { display:block; color:#9FB0C0; font-size:.64rem; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }
.path-node strong { display:block; color:#FFFFFF; font-size:1rem; margin-top:4px; }
.path-arrow { text-align:center; color:#67E3D7; font-size:1.6rem; font-weight:950; }
.path-strip { margin-top:13px; display:grid; grid-template-columns:repeat(3,1fr); gap:8px; position:relative; z-index:1; }
.path-mini { color:#DCE5ED; font-size:.67rem; text-align:center; padding:9px 7px; border-radius:10px; background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.09); }

/* Cards / metrics */
.section-card { background:#FFFFFF; border:1px solid var(--line); border-radius:16px; padding:20px; box-shadow:var(--shadow-sm); margin-bottom:16px; }
.section-title { color:var(--ink); font-size:1.02rem; font-weight:900; letter-spacing:-.018em; margin-bottom:3px; }
.section-subtitle { color:var(--muted); font-size:.8rem; margin-bottom:12px; }
.metric-card { position:relative; overflow:hidden; background:#FFFFFF; border:1px solid var(--line); border-radius:14px; padding:15px 16px 14px 17px; min-height:104px; box-shadow:var(--shadow-sm); }
.metric-card:before { content:""; position:absolute; left:0; top:0; bottom:0; width:4px; background:var(--metric-accent,var(--teal)); }
.metric-card.metric-teal{--metric-accent:var(--teal)} .metric-card.metric-cyan{--metric-accent:var(--cyan)}
.metric-card.metric-violet{--metric-accent:var(--violet)} .metric-card.metric-amber{--metric-accent:var(--amber)}
.metric-card.metric-coral{--metric-accent:var(--coral)} .metric-card.metric-green{--metric-accent:var(--green)}
.metric-label { font-size:.64rem; text-transform:uppercase; letter-spacing:.075em; font-weight:900; color:var(--muted); }
.metric-value { font-size:1.58rem; line-height:1.1; font-weight:950; color:var(--ink); margin:8px 0 4px; letter-spacing:-.035em; }
.metric-note { font-size:.72rem; color:var(--muted); line-height:1.35; }

.status-pill { display:inline-flex; align-items:center; gap:6px; border-radius:999px; padding:5px 9px; font-size:.69rem; font-weight:850; white-space:nowrap; }
.status-dot { width:6px; height:6px; border-radius:50%; background:currentColor; }
.pill-success { color:#087A4C; background:var(--green-soft); }
.pill-wait { color:#A76000; background:var(--amber-soft); }
.pill-action { color:#006F6A; background:var(--teal-soft); }
.pill-info { color:#08739A; background:var(--cyan-soft); }
.pill-danger { color:#B23A2D; background:var(--coral-soft); }
.pill-accent { color:#5E43BA; background:var(--violet-soft); }
.pill-neutral { color:#536273; background:#EEF2F6; }

/* Stage navigator */
.workflow { display:grid; grid-template-columns:repeat(5,1fr); gap:10px; margin:11px 0 23px; }
.workflow-step { position:relative; overflow:hidden; padding:15px 15px 13px; min-width:0; background:#FFFFFF; border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow-sm); }
.workflow-step:before { content:""; position:absolute; top:0; left:0; right:0; height:4px; background:var(--stage-accent,var(--teal)); }
.workflow-step:nth-child(1){--stage-accent:var(--cyan)} .workflow-step:nth-child(2){--stage-accent:var(--teal)}
.workflow-step:nth-child(3){--stage-accent:var(--violet)} .workflow-step:nth-child(4){--stage-accent:var(--amber)}
.workflow-step:nth-child(5){--stage-accent:var(--coral)}
.workflow-top { display:flex; align-items:flex-start; justify-content:space-between; gap:8px; margin-bottom:6px; }
.workflow-number { color:var(--muted-2); font-size:.59rem; font-weight:900; letter-spacing:.08em; text-transform:uppercase; }
.workflow-name { color:var(--ink); font-size:.88rem; font-weight:900; margin-top:2px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.workflow-percent { color:var(--ink-2); font-size:.72rem; font-weight:900; }
.workflow-track { height:5px; background:#E8EDF2; border-radius:999px; overflow:hidden; margin:7px 0 9px; }
.workflow-fill { height:100%; background:var(--stage-accent,var(--teal)); border-radius:999px; }

/* Progress */
.progress-panel { background:linear-gradient(180deg,#FFFFFF 0%,#FBFCFE 100%); border:1px solid var(--line); border-radius:16px; padding:17px 18px; margin:10px 0 16px; box-shadow:var(--shadow-sm); }
.progress-panel-top { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
.progress-panel-title { color:var(--ink); font-size:.92rem; font-weight:900; }
.progress-panel-meta { color:var(--muted); font-size:.75rem; margin-top:3px; }
.progress-panel-value { color:var(--teal-dark); font-size:1.12rem; font-weight:950; white-space:nowrap; }
.progress-track { height:10px; background:#E7EDF2; border-radius:999px; overflow:hidden; margin:12px 0 9px; }
.progress-fill { height:100%; background:linear-gradient(90deg,var(--teal) 0%,var(--cyan) 58%,#6A5BD4 100%); border-radius:999px; box-shadow:0 0 14px rgba(14,156,203,.18); }
.progress-foot { display:flex; justify-content:space-between; gap:16px; color:var(--muted); font-size:.72rem; }

.info-box,.success-box,.warning-box,.danger-box { border-radius:12px; padding:13px 15px; margin:9px 0 13px; border:1px solid; font-size:.82rem; line-height:1.45; }
.info-box { background:var(--cyan-soft); border-color:#B9E8F7; color:#0B6280; }
.success-box { background:var(--green-soft); border-color:#BFEBD6; color:#0B7048; }
.warning-box { background:var(--amber-soft); border-color:#F6DCA6; color:#915800; }
.danger-box { background:var(--coral-soft); border-color:#F5C7C0; color:#A63A2D; }
.download-strip { background:#FFFFFF; border:1px solid var(--line); border-radius:12px; padding:11px 13px; margin:9px 0 14px; box-shadow:var(--shadow-sm); }

.activity { border-left:2px solid #D7E2E9; margin-left:8px; padding-left:18px; }
.activity-item { position:relative; margin:0 0 14px; }
.activity-item:before { content:""; position:absolute; left:-24px; top:5px; width:9px; height:9px; border-radius:50%; background:var(--teal); box-shadow:0 0 0 4px var(--canvas); }
.activity-title { font-weight:850; color:var(--ink); }
.activity-meta { color:var(--muted); font-size:.74rem; margin-top:2px; }

/* Buttons: white text is forced on primary controls */
div[data-testid="stButton"] > button,
div[data-testid="stFormSubmitButton"] > button,
div[data-testid="stDownloadButton"] > button {
  border-radius:10px; border:1px solid #C9D3DD; font-weight:780; min-height:40px; box-shadow:none;
  background:#FFFFFF; color:var(--ink) !important; transition:all .14s ease;
}
div[data-testid="stButton"] > button:hover,
div[data-testid="stDownloadButton"] > button:hover { transform:translateY(-1px); border-color:#AAB7C3; box-shadow:0 5px 14px rgba(29,45,58,.06); }
div[data-testid="stButton"] > button[kind="primary"],
div[data-testid="stFormSubmitButton"] > button[kind="primary"],
div[data-testid="stDownloadButton"] > button[kind="primary"] {
  background:linear-gradient(100deg,#008C85 0%,#0E9CCB 120%) !important; border-color:#008C85 !important; color:#FFFFFF !important;
  box-shadow:0 7px 18px rgba(0,140,133,.17);
}
div[data-testid="stButton"] > button[kind="primary"] *,
div[data-testid="stFormSubmitButton"] > button[kind="primary"] *,
div[data-testid="stDownloadButton"] > button[kind="primary"] * { color:#FFFFFF !important; }
div[data-testid="stButton"] > button[kind="primary"]:hover,
div[data-testid="stFormSubmitButton"] > button[kind="primary"]:hover,
div[data-testid="stDownloadButton"] > button[kind="primary"]:hover {
  background:linear-gradient(100deg,#007A74 0%,#0A87B1 120%) !important; color:#FFFFFF !important; transform:translateY(-1px);
}

/* Streamlit versions that expose base-button test ids instead of kind attributes */
[data-testid="stBaseButton-primary"] {
  background:linear-gradient(100deg,#008C85 0%,#0E9CCB 120%) !important;
  border-color:#008C85 !important;
  color:#FFFFFF !important;
  font-weight:800 !important;
}
[data-testid="stBaseButton-primary"] * { color:#FFFFFF !important; }
[data-testid="stBaseButton-primary"]:hover {
  background:linear-gradient(100deg,#007A74 0%,#0A87B1 120%) !important;
  color:#FFFFFF !important;
}
[data-testid="stBaseButton-secondary"] { color:var(--ink) !important; }

/* Stronger boundaries around Streamlit bordered containers */
[data-testid="stVerticalBlockBorderWrapper"] {
  border-color:#D3DDE6 !important;
  border-radius:14px !important;
  background:#FFFFFF !important;
  box-shadow:var(--shadow-sm);
}

/* Tabs */
[data-testid="stTabs"] [data-baseweb="tab-list"] { gap:6px; background:#E9EEF4; padding:5px; border-radius:13px; width:100%; overflow-x:auto; }
[data-testid="stTabs"] button[data-baseweb="tab"] { height:39px; padding:0 16px; border-radius:9px; color:#5A6978; font-weight:800; background:transparent; }
[data-testid="stTabs"] button[data-baseweb="tab"][aria-selected="true"] { color:#0A706C !important; background:#FFFFFF !important; box-shadow:0 1px 5px rgba(31,47,61,.08); }
[data-testid="stTabs"] [data-baseweb="tab-highlight"] { display:none; }

/* Inputs / tables */
[data-testid="stDataFrame"], [data-testid="stDataEditor"] { border:1px solid #CCD6E0; border-radius:12px; overflow:hidden; background:#FFFFFF; box-shadow:var(--shadow-sm); }
[data-testid="stFileUploaderDropzone"] { background:#FFFFFF; border:1.5px dashed #9FAEBC; border-radius:12px; }
[data-testid="stExpander"] { background:#FFFFFF; border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow-sm); }
[data-testid="stProgress"] > div > div > div > div { background:linear-gradient(90deg,var(--teal),var(--cyan)); }
textarea, input { border-color:#C9D3DD !important; background:#FFFFFF !important; color:var(--ink) !important; }
[data-baseweb="input"] > div, [data-baseweb="textarea"] { border-radius:10px !important; }

/* Dialog */
[role="dialog"] { border-radius:20px !important; box-shadow:0 28px 70px rgba(22,34,45,.22) !important; overflow:hidden; }
[role="dialog"] > div { background:#F7F9FC !important; }
[role="dialog"] h2 { font-size:1.55rem !important; font-weight:950 !important; color:var(--ink) !important; letter-spacing:-.035em !important; }
.modal-intro { background:linear-gradient(115deg,#17212B 0%,#006F6A 135%); color:#FFFFFF; border-radius:14px; padding:16px 18px; margin:2px 0 16px; }
.modal-intro strong { display:block; color:#FFFFFF; font-size:.92rem; }
.modal-intro span { display:block; color:#CDE1E2; font-size:.76rem; line-height:1.45; margin-top:4px; }

h1,h2,h3,h4 { color:var(--ink); letter-spacing:-.022em; }
p, li, label { color:var(--ink); }

@media (max-width:1100px) {
  .app-hero { grid-template-columns:1fr; }
  .hero-right { grid-template-columns:repeat(4,1fr); }
  .run-banner { grid-template-columns:1fr 1fr; }
  .run-cell:nth-child(2){border-right:none}
  .workflow { grid-template-columns:repeat(2,1fr); }
  .empty-shell { grid-template-columns:1fr; }
}
@media (max-width:720px) {
  .block-container { padding-left:1rem; padding-right:1rem; }
  .app-hero { padding:24px 20px; border-radius:16px; }
  .hero-title { font-size:1.72rem; }
  .hero-right { grid-template-columns:1fr 1fr; }
  .workflow { grid-template-columns:1fr; }
  .run-banner { grid-template-columns:1fr; }
  .run-cell { border-right:none; border-bottom:1px solid var(--line-soft); }
  .run-cell:last-child{border-bottom:none}
}
</style>
""",
        unsafe_allow_html=True,
    )


def app_header(version: str) -> None:
    st.markdown(
        f"""
<div class="app-hero">
  <div class="hero-left">
    <div class="hero-kicker">Migration control center · v{escape(version)}</div>
    <div class="hero-title">SIoT → EIoT Time-Series Migration</div>
    <div class="hero-subtitle">Move large historical datasets with controlled checkpoints, row-level selection, live status tracking and customer-ready audit reports.</div>
    <div class="hero-badges">
      <span class="hero-badge"><span class="hero-badge-dot"></span>Resumable</span>
      <span class="hero-badge"><span class="hero-badge-dot"></span>10s live refresh</span>
      <span class="hero-badge"><span class="hero-badge-dot"></span>Row controlled</span>
      <span class="hero-badge"><span class="hero-badge-dot"></span>Auditable</span>
    </div>
  </div>
  <div class="hero-right">
    <div class="hero-mini"><div class="hero-mini-value">SIoT</div><div class="hero-mini-label">Source metadata + Cold Store history</div></div>
    <div class="hero-mini"><div class="hero-mini-value">EIoT</div><div class="hero-mini-label">Target mapping + ingestion</div></div>
    <div class="hero-mini"><div class="hero-mini-value">↻</div><div class="hero-mini-label">Checkpointed restart after interruption</div></div>
    <div class="hero-mini"><div class="hero-mini-value">✓</div><div class="hero-mini-label">Downloadable evidence at every phase</div></div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def phase_header(kicker: str, title: str, subtitle: str, *, tone: str = "teal", chip: str | None = None) -> None:
    chip_html = f'<span class="phase-chip">{escape(chip)}</span>' if chip else ""
    st.markdown(
        f"""<div class="phase-header phase-{escape(tone)}">
          <div><div class="phase-kicker">{escape(kicker)}</div><div class="phase-title">{escape(title)}</div><div class="phase-subtitle">{escape(subtitle)}</div></div>
          {chip_html}
        </div>""",
        unsafe_allow_html=True,
    )


def empty_state() -> None:
    st.markdown(
        """
<div class="empty-shell">
  <div class="empty-card">
    <div class="empty-kicker">Ready for a new migration</div>
    <div class="empty-title">Start with the business scope. The app handles the technical orchestration.</div>
    <div class="empty-copy">Paste Technical Objects or upload the customer workbook, choose the migration period, then review and control every stage before anything moves.</div>
    <div class="empty-feature-grid">
      <div class="empty-feature"><strong>Checkpointed execution</strong><span>Completed work survives browser, network or machine interruptions.</span></div>
      <div class="empty-feature"><strong>Selective migration</strong><span>Every actionable row is selected by default and can be deselected.</span></div>
      <div class="empty-feature"><strong>Live progress</strong><span>Stage-level progress, ETA estimates and automatic status refresh.</span></div>
      <div class="empty-feature"><strong>Customer evidence</strong><span>Download complete, pending and validation details without screenshots.</span></div>
    </div>
  </div>
  <div class="migration-visual">
    <div class="path-row">
      <div class="path-node"><small>Source</small><strong>SIoT Cold Store</strong></div>
      <div class="path-arrow">→</div>
      <div class="path-node"><small>Target</small><strong>EIoT</strong></div>
    </div>
    <div class="path-strip">
      <div class="path-mini">Discover & map</div>
      <div class="path-mini">Transform safely</div>
      <div class="path-mini">Validate & load</div>
    </div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )


def run_summary_banner(run_id: str, to_count: int, period: str, storage: str) -> None:
    st.markdown(
        f"""<div class="run-banner">
          <div class="run-cell run-main"><div class="run-label">Current migration</div><div class="run-value">{escape(run_id)}</div></div>
          <div class="run-cell"><div class="run-label">Technical Objects</div><div class="run-value">{to_count:,}</div></div>
          <div class="run-cell"><div class="run-label">Migration period</div><div class="run-value">{escape(period)}</div></div>
          <div class="run-cell"><div class="run-label">Local working data</div><div class="run-value">{escape(storage)}</div></div>
        </div>""",
        unsafe_allow_html=True,
    )
def status_pill(label: str, tone: str = "neutral") -> str:
    cls = {
        "success": "pill-success",
        "wait": "pill-wait",
        "action": "pill-action",
        "info": "pill-info",
        "danger": "pill-danger",
        "accent": "pill-accent",
        "neutral": "pill-neutral",
    }.get(tone, "pill-neutral")
    return f'<span class="status-pill {cls}"><span class="status-dot"></span>{escape(str(label))}</span>'


def friendly_stage(raw: str | None) -> tuple[str, str, str]:
    value = (raw or "NOT_STARTED").upper()
    mapping = {
        "NOT_STARTED": ("Not started", "neutral", "No work has started."),
        "READY": ("Ready", "action", "Eligible items are ready for the next action."),
        "RUNNING": ("In progress", "info", "Work is currently running."),
        "READY_TO_DOWNLOAD": ("Download ready", "action", "SAP has prepared at least one export."),
        "PARTIAL": ("Partially complete", "accent", "Completed items are preserved; others remain available."),
        "PARTIAL_WITH_ISSUES": ("Partial · review", "wait", "Completed items are preserved; some items need review."),
        "COMPLETE": ("Completed", "success", "All applicable items completed."),
        "COMPLETE_WITH_ISSUES": ("Completed with notes", "wait", "The phase completed; review noted items."),
        "PROCESSED": ("Processed", "success", "EIoT processed the validation file."),
        "PROCESSING": ("Processing", "info", "EIoT is processing the file."),
        "APPROVED": ("Approved", "success", "Human approval has been recorded."),
        "IN_PROGRESS": ("In progress", "info", "Production upload/processing is active."),
        "ATTENTION_REQUIRED": ("Needs attention", "danger", "One or more items require review."),
        "NEEDS_ATTENTION": ("Needs attention", "danger", "One or more items require review."),
        "FAILED": ("Needs attention", "danger", "One or more items failed."),
    }
    return mapping.get(value, (raw or "Unknown", "neutral", f"Backend status: {raw or 'Unknown'}"))


def friendly_extraction(raw: str | None) -> tuple[str, str, str]:
    value = str(raw or "Unknown")
    low = value.lower()
    if value == "Downloaded":
        return "Downloaded", "success", "Downloaded and unpacked locally; CSV files can be transformed."
    if value == "Ready for Download":
        return "Ready to download", "action", "SAP has finished preparing this export."
    if value == "NO_DATA":
        return "No source data", "neutral", "SAP reported no time-series data for this position/date range."
    if value == "SPLIT":
        return "Split automatically", "info", "The original range was too large; smaller child jobs were created."
    if value in {"FAILED", "Failed", "Exception", "Expired"}:
        return "Needs attention", "danger", "This item did not complete successfully."
    if value == "PLANNED":
        return "Not started", "neutral", "Select this row when you want to start it."
    if "submitted" in low or "initiated" in low or value == "Initiated":
        return "SAP preparing", "wait", "SAP accepted the request and is preparing the export."
    return value, "info", f"SAP backend status: {value}"


def friendly_transform(raw: str | None) -> tuple[str, str, str]:
    value = str(raw or "READY")
    mapping = {
        "READY": ("Ready to transform", "action", "Downloaded CSV is available locally."),
        "RUNNING": ("Transforming", "info", "The selected CSV is being converted."),
        "SUCCESS": ("Transformed", "success", "A validated Parquet output was created."),
        "NO_DATA": ("No selected data", "neutral", "No rows for the selected TO scope were found."),
        "NO_MAPPINGS": ("No valid mapping", "wait", "No supported target mapping was available."),
        "FAILED": ("Needs attention", "danger", "Transformation failed for this file."),
    }
    return mapping.get(value, (value, "neutral", f"Backend status: {value}"))


def friendly_upload(raw: str | None) -> tuple[str, str, str]:
    value = str(raw or "READY")
    mapping = {
        "READY": ("Ready to upload", "action", "Prepared locally but not yet sent to EIoT."),
        "UPLOADED": ("Accepted by EIoT", "info", "The file was accepted; processing has not finished."),
        "Received": ("Accepted by EIoT", "info", "EIoT received the file and is preparing it for processing."),
        "Scanned": ("Validated", "accent", "The file passed EIoT validation and is waiting for ingestion."),
        "In Process": ("Ingesting", "wait", "EIoT is ingesting the time-series data."),
        "Processed": ("Successfully completed", "success", "EIoT processed and ingested this file successfully."),
        "Processing Failed": ("Needs attention", "danger", "EIoT could not process this file."),
        "FAILED": ("Upload failed", "danger", "The file could not be submitted to EIoT."),
    }
    return mapping.get(value, (value, "info", f"EIoT backend status: {value}"))


def metric_card(label: str, value: Any, note: str = "", tone: str | None = None) -> None:
    if tone is None:
        low = str(label).lower()
        if any(word in low for word in ("attention", "failed", "issue", "error")):
            tone = "coral"
        elif any(word in low for word in ("pending", "preparing", "ready")):
            tone = "amber"
        elif any(word in low for word in ("processed", "completed", "downloaded", "transformed", "discovered", "success")):
            tone = "green"
        elif any(word in low for word in ("target", "mapping", "characteristic", "validation")):
            tone = "violet"
        elif any(word in low for word in ("csv", "job", "file", "technical")):
            tone = "cyan"
        else:
            tone = "teal"
    st.markdown(
        f'<div class="metric-card metric-{escape(tone)}"><div class="metric-label">{escape(str(label))}</div>'
        f'<div class="metric-value">{escape(str(value))}</div>'
        f'<div class="metric-note">{escape(str(note))}</div></div>',
        unsafe_allow_html=True,
    )


def stage_strip(run: dict[str, Any], progress: dict[str, float] | None = None) -> None:
    progress = progress or {}
    items = [
        ("01", "Scope", run.get("discovery_status"), progress.get("Scope", 0.0)),
        ("02", "Extraction", run.get("extraction_status"), progress.get("Extraction", 0.0)),
        ("03", "Transform", run.get("transform_status"), progress.get("Transform", 0.0)),
        ("04", "Validation", run.get("validation_status"), progress.get("Validation", 0.0)),
        ("05", "Production", run.get("load_status"), progress.get("Production", 0.0)),
    ]
    html = ['<div class="workflow">']
    for number, name, raw, pct in items:
        label, tone, _ = friendly_stage(raw)
        pct = max(0.0, min(float(pct or 0.0), 1.0))
        html.append(
            '<div class="workflow-step">'
            '<div class="workflow-top">'
            f'<div><div class="workflow-number">Stage {number}</div><div class="workflow-name">{escape(name)}</div></div>'
            f'<div class="workflow-percent">{pct*100:.0f}%</div>'
            '</div>'
            f'<div class="workflow-track"><div class="workflow-fill" style="width:{pct*100:.1f}%"></div></div>'
            f'{status_pill(label, tone)}'
            '</div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def progress_panel(
    title: str,
    completed: int,
    total: int,
    *,
    eta: str = "—",
    last_updated: str = "—",
    note: str = "",
) -> None:
    total = max(int(total or 0), 0)
    completed = max(0, min(int(completed or 0), total if total else int(completed or 0)))
    pct = (completed / total) if total else 0.0
    suffix = f" · {escape(note)}" if note else ""
    st.markdown(
        '<div class="progress-panel">'
        '<div class="progress-panel-top">'
        f'<div><div class="progress-panel-title">{escape(title)}</div>'
        f'<div class="progress-panel-meta">{completed:,} of {total:,} complete{suffix}</div></div>'
        f'<div class="progress-panel-value">{pct*100:.0f}%</div>'
        '</div>'
        f'<div class="progress-track"><div class="progress-fill" style="width:{pct*100:.1f}%"></div></div>'
        '<div class="progress-foot">'
        f'<span>Estimated remaining: {escape(eta)}</span>'
        f'<span>Last updated: {escape(last_updated)}</span>'
        '</div>'
        '</div>',
        unsafe_allow_html=True,
    )


def activity_timeline(events: list[dict[str, Any]], limit: int = 8) -> None:
    if not events:
        st.caption("No activity recorded yet.")
        return
    html = ['<div class="activity">']
    for event in events[:limit]:
        created = event.get("created_at") or ""
        try:
            stamp = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%d %b %H:%M")
        except Exception:
            stamp = created
        detail = event.get("detail") or ""
        html.append(
            '<div class="activity-item">'
            f'<div class="activity-title">{escape(str(event.get("title") or "Activity"))}</div>'
            f'<div class="activity-meta">{escape(stamp)} · {escape(str(event.get("stage") or ""))}'
            f'{(" · " + escape(str(detail))) if detail else ""}</div>'
            '</div>'
        )
    html.append("</div>")
    st.markdown("".join(html), unsafe_allow_html=True)


def format_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.0f} {units[index]}" if index == 0 else f"{size:.1f} {units[index]}"
