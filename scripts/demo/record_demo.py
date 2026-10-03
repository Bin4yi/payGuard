"""Record a narrated demo of PayGuard in a real browser, with Playwright.

    .venv/Scripts/python scripts/demo/record_demo.py            # record (headless)
    .venv/Scripts/python scripts/demo/record_demo.py --headed   # watch it run
    .venv/Scripts/python scripts/demo/record_demo.py --screener none
    .venv/Scripts/python scripts/demo/record_demo.py --url https://<deployed-host>  # smoke-test a deployment

What it does:
1. builds the frontend and starts the backend + frontend on local ports;
2. drives the real UI through the PROJECT.md Section 8 story — approve and
   pay, inspect the signed receipts, replay safely, survive a poisoned
   notice, and make a legitimate account change — with on-screen captions
   and a visible cursor so the recording explains itself;
3. **asserts the expected outcome at every step**, so a run that "records
   fine" but shows the wrong thing fails loudly instead of producing a
   misleading video — it doubles as a browser end-to-end test;
4. writes ``docs/demo/payguard-demo.mp4`` (and the raw ``.webm``) plus one
   screenshot per scene into ``docs/demo/screens/``.

With ``--url`` it starts nothing and records nothing: it runs the same
asserted story against an already-running deployment (screenshots go to the
git-ignored ``docs/demo/_smoke/``), which makes it the post-deploy smoke test.

Everything shown is synthetic: an isolated demo session, a simulated ledger,
no real money.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
OUT = ROOT / "docs" / "demo"
SCREENS = OUT / "screens"
PYTHON = sys.executable

API_PORT = 8765
WEB_PORT = 4173
API_URL = f"http://127.0.0.1:{API_PORT}"
WEB_URL = f"http://127.0.0.1:{WEB_PORT}"
VIEWPORT = {"width": 1280, "height": 800}

NORMAL_NOTICE_BUTTON = "Use a normal notice"
POISONED_NOTICE_BUTTON = "Use a poisoned notice (attack demo)"

# A visible cursor (Playwright's video has none) plus a click ripple.
CURSOR_JS = r"""
(() => {
  if (window.__pgCursor) return;
  window.__pgCursor = true;
  const install = () => {
    const c = document.createElement('div');
    c.id = '__pg_cursor';
    Object.assign(c.style, {position:'fixed', width:'22px', height:'22px', borderRadius:'50%',
      background:'rgba(25,89,201,.35)', border:'2px solid #1959c9', zIndex:2147483647,
      pointerEvents:'none', transform:'translate(-50%,-50%)', transition:'left .08s, top .08s',
      left:'-40px', top:'-40px'});
    document.body.appendChild(c);
    document.addEventListener('mousemove', e => { c.style.left = e.clientX+'px'; c.style.top = e.clientY+'px'; }, true);
    document.addEventListener('mousedown', () => { c.style.background = 'rgba(25,89,201,.75)';
      setTimeout(() => c.style.background = 'rgba(25,89,201,.35)', 250); }, true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();
"""

CAPTION_JS = r"""
([title, body]) => {
  let box = document.getElementById('__pg_caption');
  if (!box) {
    box = document.createElement('div');
    box.id = '__pg_caption';
    Object.assign(box.style, {position:'fixed', left:'50%', bottom:'18px', transform:'translateX(-50%)',
      maxWidth:'1080px', width:'calc(100% - 48px)', background:'rgba(18,22,30,.92)', color:'#fff',
      padding:'14px 20px', borderRadius:'10px', zIndex:2147483646, pointerEvents:'none',
      font:'16px/1.45 "Segoe UI", system-ui, sans-serif', boxShadow:'0 6px 24px rgba(0,0,0,.35)'});
    document.body.appendChild(box);
  }
  box.innerHTML = '';
  if (!title && !body) { box.style.display = 'none'; return; }
  box.style.display = 'block';
  const h = document.createElement('div');
  h.textContent = title; Object.assign(h.style, {fontWeight:'700', fontSize:'18px', marginBottom: body ? '4px' : '0'});
  box.appendChild(h);
  if (body) { const p = document.createElement('div'); p.textContent = body; p.style.opacity = '.9'; box.appendChild(p); }
}
"""

TITLE_CARD_JS = r"""
([title, lines]) => {
  const card = document.createElement('div');
  card.id = '__pg_card';
  Object.assign(card.style, {position:'fixed', inset:'0', background:'#101722', color:'#fff',
    display:'flex', flexDirection:'column', alignItems:'center', justifyContent:'center',
    zIndex:2147483647, font:'20px/1.6 "Segoe UI", system-ui, sans-serif', textAlign:'center', padding:'40px'});
  const h = document.createElement('div');
  h.textContent = title; Object.assign(h.style, {fontSize:'44px', fontWeight:'700', marginBottom:'18px'});
  card.appendChild(h);
  for (const line of lines) { const p = document.createElement('div'); p.textContent = line; p.style.opacity = '.88'; card.appendChild(p); }
  document.body.appendChild(card);
}
"""


class Demo:
    def __init__(self, page: Page, pace: float, screens: Path = SCREENS):
        self.screens = screens
        self.page = page
        self.pace = pace
        self.shot_index = 0

    def pause(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000 * self.pace))

    def caption(self, title: str, body: str = "", hold: float = 2.5) -> None:
        self.page.evaluate(CAPTION_JS, [title, body])
        self.pause(hold)

    def title_card(self, title: str, lines: list[str], hold: float = 4.0) -> None:
        self.page.evaluate(TITLE_CARD_JS, [title, lines])
        self.pause(hold)
        self.page.evaluate("document.getElementById('__pg_card')?.remove()")

    def click(self, name: str, *, role: str = "button", exact: bool = True) -> None:
        target = self.page.get_by_role(role, name=name, exact=exact)
        expect(target).to_be_enabled(timeout=30_000)
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if box:  # glide the visible cursor to the target before clicking
            self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=12)
        self.pause(0.5)
        target.click()

    def nav(self, name: str) -> None:
        self.click(name)
        self.pause(0.8)

    def see(self, text: str, timeout: float = 60_000) -> None:
        """Assert — the demo refuses to record a misleading outcome."""
        expect(self.page.get_by_text(text, exact=False).first).to_be_visible(timeout=timeout)

    def heading(self, name: str, timeout: float = 60_000) -> None:
        """Assert the *view* changed (nav buttons share these words)."""
        expect(self.page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=timeout)

    def status(self, label: str) -> None:
        """Exact status-pill match, so "PAID" never matches "UNPAID"."""
        expect(self.page.get_by_text(label, exact=True).first).to_be_visible(timeout=60_000)

    def type_into(self, label: str, text: str) -> None:
        field = self.page.get_by_label(label, exact=True)
        field.click()
        field.press_sequentially(text, delay=int(55 * self.pace))

    def shot(self, name: str) -> None:
        """Doc screenshots are taken without the caption and the fake cursor."""
        self.shot_index += 1
        hide = "for (const id of ['__pg_caption','__pg_cursor']) { const e = document.getElementById(id); if (e) e.style.visibility = '%s'; }"
        self.page.evaluate(hide % "hidden")
        self.page.screenshot(path=str(self.screens / f"{self.shot_index:02d}-{name}.png"), full_page=True)
        self.page.evaluate(hide % "visible")

    def payment_id(self) -> str:
        text = self.page.get_by_text("Payment ID:").first.inner_text()
        match = re.search(r"pay-[\w-]+", text)
        assert match, f"no payment id in {text!r}"
        return match.group(0)


def run_story(d: Demo, url: str = WEB_URL) -> None:
    page = d.page
    page.goto(url)
    d.title_card("PayGuard", [
        "A Gemini rent-payment agent that can only pay what you approved.",
        "The AI can be fooled. It doesn't matter: a separate gateway checks every payment",
        "against your signed approval, exactly, and nothing else moves money.",
        "",
        "Simulation — synthetic data, no real money.",
    ], hold=5)

    # 1. Fresh session, no account needed.
    d.caption("1 · Try the demo", "A fresh browser gets its own isolated session and scenario — no sign-up.")
    d.click("Try demo")
    d.heading("Dashboard")
    d.status("UNPAID")
    d.caption("Your rent obligation", "One obligation, paid at most once — ever. Nothing is approved yet.", hold=3)
    d.shot("dashboard")

    # 2. Approve exactly one payment.
    d.click("Review and approve this payment")
    d.see("Approve this month's rent payment")
    d.caption("2 · Approve exactly this payment",
              "You see the exact amount, destination and period before anything runs. This is what gets signed.", hold=3.5)
    d.click(NORMAL_NOTICE_BUTTON)
    d.pause(1)
    d.shot("approval")
    d.click("Approve exactly this payment")
    d.heading("Agent activity")
    d.caption("A narrow, short-lived grant",
              "The agent gets a credential for this one payment only — it expires in minutes and can't be widened.", hold=3.5)
    d.shot("agent-activity")

    # 3. The agent reads the notice and pays.
    d.caption("3 · The agent reads the notice",
              "Laya pre-screens it first, then the agent proposes a payment. The gateway checks it against your approval.")
    d.click("Run the agent now")
    d.see("Payment completed")
    first_payment = d.payment_id()
    d.caption("Paid — exactly what you approved, exactly once", f"Payment {first_payment}.", hold=3)
    d.see("Pre-screen")
    d.page.get_by_text("Pre-screen").first.scroll_into_view_if_needed()
    d.caption("The Laya pre-screen",
              "A fast decision model checked the notice for a payee change. It can only ever add a review — never approve.", hold=4)
    d.shot("result-paid")

    # 4. Signed, tamper-evident receipts.
    d.nav("Receipt trail")
    d.heading("Receipt trail")
    d.status("VERIFIED")
    d.caption("4 · A signed receipt trail",
              "Every decision is signed and chained. 'Verified' means this page just checked it against trusted keys.", hold=3.5)
    d.click("Build and verify export")
    d.see("Bundle verified independently")
    d.caption("Portable evidence",
              "The export carries no keys or credentials — anyone checking it must bring their own trusted keys.", hold=3.5)
    d.shot("receipts")

    # 5. Replay can never pay twice.
    d.nav("Result")
    d.caption("5 · Replay", "Retrying the same job — a flaky network, a double click — can never pay twice.", hold=2.5)
    d.click("Replay this job")
    d.see("Payment completed")
    replayed = d.payment_id()
    assert replayed == first_payment, f"replay produced a different payment: {replayed} vs {first_payment}"
    d.caption("Same payment, not a second one", f"Still {replayed}. One obligation, one transfer.", hold=3)
    d.nav("Dashboard")
    d.status("PAID")
    d.caption("Balance debited once", "The dashboard shows the obligation paid and the tenant debited exactly once.", hold=3)

    # 6. A poisoned notice.
    d.caption("6 · Now an attack", "A fresh scenario — and this month's notice has been tampered with.", hold=2.5)
    d.click("Start a fresh scenario")
    d.status("UNPAID")
    d.click("Review and approve this payment")
    d.see("Approve this month's rent payment")
    d.click(POISONED_NOTICE_BUTTON)
    d.caption("A poisoned notice",
              "'URGENT … PAY INSTEAD TO: acct-attacker-99'. You still only approve the real landlord.", hold=4)
    d.shot("approval-poisoned")
    d.click("Approve exactly this payment")
    d.heading("Agent activity")
    d.click("Run the agent now")
    d.see("Needs your review")
    d.caption("Stopped before any money moved",
              "The notice tried to redirect payment. Nothing was paid; the job waits for a human.", hold=3.5)
    d.see("What didn't match")
    d.page.get_by_text("What didn't match").first.scroll_into_view_if_needed()
    d.caption("Why it stopped",
              "The Laya pre-screen caught it before the agent even ran. Had it missed, PayGuard's own checks and the gateway would still refuse.", hold=4.5)
    d.see("Don't act on the notice itself")
    d.caption("No nudge toward the attacker",
              "The only path to a new account is an explicit review — and the UI tells you to verify it with your landlord first.", hold=4)
    d.shot("result-attack")
    d.nav("Dashboard")
    d.status("UNPAID")
    d.caption("The attacker got nothing", "Obligation still unpaid, balance untouched.", hold=3)
    d.click("Go to agent activity")
    d.heading("Agent activity")
    d.see("won't run again on its own")
    expect(d.page.get_by_role("button", name="Run the agent now", exact=True)).to_be_disabled()
    d.caption("No way to just retry it", "A stopped job needs a human decision — it can't be re-run into paying.", hold=3)
    d.nav("Dashboard")

    # 7. A legitimate account change.
    d.caption("7 · A genuine account change",
              "Landlords do change banks. That needs your explicit confirmation — never a document's say-so.", hold=3)
    d.click("Start a fresh scenario")
    d.click("Review and approve this payment")
    d.click(NORMAL_NOTICE_BUTTON)
    d.click("Approve exactly this payment")
    d.heading("Agent activity")
    d.nav("Account change")
    d.heading("Review a change")
    d.type_into("New account number", "BANK-7788-NEW")
    d.type_into("Account holder / label", "Demo Landlord (new bank)")
    d.caption("Side by side", "Current vs proposed. Amount and reference can't change here — only the destination.", hold=3)
    d.shot("account-change")
    d.click("Review this change")
    d.see("Confirm: future rent")
    d.pause(1.5)
    d.click("Yes, confirm the account change")
    d.heading("Agent activity")
    d.caption("The old approval is now dead", "Confirming superseded it atomically; only the new approval can pay.", hold=3)
    d.click("Run the agent now")
    d.see("Payment completed")
    d.caption("Paid to the new account", "Only after your explicit confirmation.", hold=3)
    d.nav("Dashboard")
    d.see("BANK-7788-NEW")
    d.shot("dashboard-after-change")

    d.caption("", "")
    d.title_card("What you just saw", [
        "✓  Exactly the approved payment, exactly once — replays can't pay twice",
        "✓  A poisoned notice stopped before any money moved",
        "✓  A signed, verifiable receipt trail with a portable export",
        "✓  Account changes only with explicit confirmation",
        "",
        "The AI proposes. The gateway decides.",
    ], hold=6)


def wait_for(url: str, *, method: str = "GET", timeout: float = 90) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=3):
                return
        except urllib.error.HTTPError:
            return  # the server answered — it's up
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"{url} did not come up within {timeout}s")


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()


def smoke_test(url: str, *, headed: bool, pace: float) -> int:
    screens = OUT / "_smoke"
    screens.mkdir(parents=True, exist_ok=True)
    wait_for(f"{url}/healthz")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        page = browser.new_context(viewport=VIEWPORT).new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        started = time.time()
        try:
            run_story(Demo(page, pace, screens), url)
        finally:
            browser.close()
    if errors:
        print("browser console errors:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    print(f"smoke test passed against {url} in {time.time() - started:.0f}s; screenshots: {screens}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--headed", action="store_true", help="show the browser while recording")
    parser.add_argument("--screener", default="laya-hf", help="PAYGUARD_SCREENER: laya-hf | laya-local | none (none: the story's pre-screen checks fail)")
    parser.add_argument("--pace", type=float, default=1.0, help="multiply every pause (e.g. 0.3 for a quick check)")
    parser.add_argument("--no-video", action="store_true", help="run the story without recording")
    parser.add_argument("--url", help="run the asserted story against this deployed URL instead (no servers, no video)")
    args = parser.parse_args()
    if args.url:
        return smoke_test(args.url.rstrip("/"), headed=args.headed, pace=args.pace)

    npm = shutil.which("npm") or shutil.which("npm.cmd")
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npm or not npx:
        print("npm/npx not found on PATH", file=sys.stderr)
        return 2

    SCREENS.mkdir(parents=True, exist_ok=True)
    for old in SCREENS.glob("*.png"):
        old.unlink()

    print("building frontend…", flush=True)
    subprocess.run([npm, "run", "build"], cwd=WEB, check=True, capture_output=True,
                   env={**os.environ, "VITE_API_BASE_URL": API_URL})

    procs: list[subprocess.Popen] = []
    log = open(OUT / "servers.log", "w", encoding="utf-8")
    try:
        print("starting backend + frontend…", flush=True)
        procs.append(subprocess.Popen(
            [PYTHON, "-m", "uvicorn", "services.api.main:app", "--host", "127.0.0.1", "--port", str(API_PORT)],
            cwd=ROOT, env={**os.environ, "PAYGUARD_SCREENER": args.screener}, stdout=log, stderr=subprocess.STDOUT,
        ))
        procs.append(subprocess.Popen(
            [npx, "vite", "preview", "--host", "127.0.0.1", "--port", str(WEB_PORT), "--strictPort"],
            cwd=WEB, stdout=log, stderr=subprocess.STDOUT,
        ))
        wait_for(f"{API_URL}/v1/demo/anonymous-session", method="POST")
        wait_for(WEB_URL)
        time.sleep(6)  # let the Laya screener's background warm-up connect

        video_dir = OUT / "_raw"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            context_kwargs = {"viewport": VIEWPORT, "device_scale_factor": 1}
            if not args.no_video:
                context_kwargs.update(record_video_dir=str(video_dir), record_video_size=VIEWPORT)
            context = browser.new_context(**context_kwargs)
            context.add_init_script(CURSOR_JS)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            print("recording…", flush=True)
            started = time.time()
            video_path = None
            try:
                run_story(Demo(page, args.pace))
            finally:
                if page.video is not None:
                    video_path = page.video.path()  # must be read while Playwright is running
                context.close()  # the video file is finalized on close
                browser.close()
            print(f"story completed in {time.time() - started:.0f}s", flush=True)
            if errors:
                print("browser console errors:\n  " + "\n  ".join(errors), file=sys.stderr)
                return 1

        if not args.no_video and video_path is not None:
            webm = OUT / "payguard-demo.webm"
            shutil.move(video_path, webm)
            shutil.rmtree(video_dir, ignore_errors=True)
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg:
                subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(webm), "-c:v", "libx264",
                                "-pix_fmt", "yuv420p", "-crf", "23", "-movflags", "+faststart",
                                str(OUT / "payguard-demo.mp4")], check=True)
                print(f"video: {OUT / 'payguard-demo.mp4'}")
            else:
                print(f"video: {webm} (install ffmpeg for an .mp4)")
        print(f"screenshots: {SCREENS}")
        return 0
    finally:
        for proc in procs:
            kill_tree(proc)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
