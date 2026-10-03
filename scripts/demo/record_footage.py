"""Record raw 1080p footage clips of PayGuard for the promo video.

    .venv/Scripts/python scripts/demo/record_footage.py http://127.0.0.1:8765

Unlike ``record_demo.py`` (one narrated, captioned walkthrough), this
records one short clip per feature with no captions, so the video edit can
add its own narration and titles. Every clip still asserts what it shows,
so the footage can't silently capture a wrong outcome.

Needs an already-running server (same origin, ideally with
PAYGUARD_SCREENER=laya-local so the pre-screen shows real scores). Clips go
to ``docs/demo/footage/<name>.webm`` (git-ignored).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "demo" / "footage"
VIEWPORT = {"width": 1440, "height": 810}
VIDEO = {"width": 1920, "height": 1080}

CURSOR_JS = r"""
(() => {
  if (window.__pgCursor) return;
  window.__pgCursor = true;
  const install = () => {
    const c = document.createElement('div');
    c.id = '__pg_cursor';
    Object.assign(c.style, {position:'fixed', width:'26px', height:'26px', borderRadius:'50%',
      background:'rgba(255,255,255,.55)', border:'2px solid rgba(79,70,229,.9)',
      boxShadow:'0 6px 18px rgba(79,70,229,.35)', backdropFilter:'blur(4px)', zIndex:2147483647,
      pointerEvents:'none', transform:'translate(-50%,-50%) scale(1)', transition:'transform .15s',
      left:'-40px', top:'-40px'});
    document.body.appendChild(c);
    document.addEventListener('mousemove', e => { c.style.left = e.clientX+'px'; c.style.top = e.clientY+'px'; }, true);
    document.addEventListener('mousedown', () => { c.style.transform = 'translate(-50%,-50%) scale(.7)';
      setTimeout(() => c.style.transform = 'translate(-50%,-50%) scale(1)', 220); }, true);
  };
  if (document.body) install(); else document.addEventListener('DOMContentLoaded', install);
})();
"""


class Clip:
    def __init__(self, page: Page):
        self.page = page

    def wait(self, seconds: float) -> None:
        self.page.wait_for_timeout(int(seconds * 1000))

    def glide(self, locator) -> None:
        locator.scroll_into_view_if_needed()
        box = locator.bounding_box()
        if box:
            self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=25)
        self.wait(0.35)

    def click(self, name: str, *, exact: bool = True) -> None:
        target = self.page.get_by_role("button", name=name, exact=exact).first
        expect(target).to_be_enabled(timeout=120_000)
        self.glide(target)
        target.click()

    def see(self, text: str, timeout: float = 120_000) -> None:
        expect(self.page.get_by_text(text, exact=False).first).to_be_visible(timeout=timeout)

    def heading(self, name: str) -> None:
        expect(self.page.get_by_role("heading", name=name, exact=True)).to_be_visible(timeout=120_000)

    def scroll_to(self, text: str) -> None:
        self.page.get_by_text(text, exact=False).first.scroll_into_view_if_needed()
        self.wait(0.6)

    def smooth_scroll(self, y: int, duration: float = 1.5) -> None:
        self.page.evaluate(
            """([y, ms]) => new Promise(r => { const s = scrollY, t0 = performance.now();
              const f = t => { const k = Math.min(1, (t - t0) / ms), e = k < .5 ? 2*k*k : 1 - (-2*k+2)**2/2;
              scrollTo(0, s + (y - s) * e); k < 1 ? requestAnimationFrame(f) : r(); }; requestAnimationFrame(f); })""",
            [y, int(duration * 1000)],
        )


def start_session(c: Clip, url: str) -> None:
    c.page.goto(url)
    c.click("Try demo")
    c.heading("Dashboard")


def approve(c: Clip, notice_button: str | None) -> None:
    c.click("Review and approve this payment")
    c.see("Approve this month's rent payment")
    if notice_button:
        c.click(notice_button)
    c.click("Approve exactly this payment")
    c.heading("Agent activity")


# ---- clips ----

def clip_welcome(c: Clip, url: str) -> None:
    c.page.goto(url)
    c.wait(2.5)
    c.page.mouse.move(700, 500, steps=20)
    c.smooth_scroll(700, 2.5)
    c.wait(2.5)
    c.smooth_scroll(0, 1.8)
    c.wait(1)


def clip_approve(c: Clip, url: str) -> None:
    start_session(c, url)
    c.wait(2)
    c.click("Review and approve this payment")
    c.see("Approve this month's rent payment")
    c.wait(2)
    c.click("Use a normal notice")
    c.wait(1)
    c.click("Approve exactly this payment")
    c.heading("Agent activity")
    c.wait(4)


def clip_pay_receipts_replay(c: Clip, url: str) -> None:
    start_session(c, url)
    approve(c, "Use a normal notice")
    c.wait(1)
    c.click("Run the agent now")
    c.see("Payment completed")
    c.wait(2.5)
    c.scroll_to("Pre-screen")
    c.wait(2)
    c.click("View receipt trail")
    c.heading("Receipt trail")
    c.see("VERIFIED")
    c.wait(2)
    c.click("Build and verify export")
    c.see("Bundle verified independently")
    c.scroll_to("Bundle verified independently")
    c.wait(2.5)
    c.click("Result")
    c.click("Replay this job")
    c.see("Payment completed")
    c.wait(2.5)


def clip_photo_attack(c: Clip, url: str) -> None:
    start_session(c, url)
    c.click("Review and approve this payment")
    c.see("Approve this month's rent payment")
    c.wait(1)
    c.click("Printed poisoned notice")
    c.see("OCR confidence", timeout=180_000)
    c.wait(1.5)
    c.scroll_to("Rent notice text")
    c.wait(2)
    c.click("Approve exactly this payment")
    c.heading("Agent activity")
    c.click("Run the agent now")
    c.see("Needs your review")
    c.wait(2)
    c.scroll_to("What didn't match")
    c.wait(2.5)
    c.scroll_to("Don't act on the notice itself")
    c.wait(2.5)


def clip_attack_lab(c: Clip, url: str) -> None:
    c.page.goto(url)
    c.click("Open the Attack Lab")
    c.heading("Attack Lab")
    c.wait(1.5)
    c.click("Run all attacks")
    expect(c.page.get_by_role("button", name="Run all attacks")).to_be_enabled(timeout=900_000)
    c.see("Laya flagged")
    c.smooth_scroll(0, 1)
    c.wait(2.5)
    c.click("Show Look-alike characters")
    c.see("Each layer, tested on its own")
    c.scroll_to("Each layer, tested on its own")
    c.wait(4)


def clip_lab_real_image(c: Clip, url: str) -> None:
    c.page.goto(url)
    c.click("Open the Attack Lab")
    c.heading("Attack Lab")
    c.scroll_to("Try your own attack")
    c.click("Real fake-invoice scam")
    c.see("OCR confidence", timeout=180_000)
    c.wait(2)
    c.click("Launch this attack")
    c.see("Each layer, tested on its own")
    c.scroll_to("Your notice")
    c.wait(4)


def clip_account_change(c: Clip, url: str) -> None:
    start_session(c, url)
    approve(c, "Use a normal notice")
    c.click("Account change")
    c.heading("Review a change")
    acct = c.page.get_by_label("New account number", exact=True)
    c.glide(acct)
    acct.click()
    acct.press_sequentially("BANK-7788-NEW", delay=60)
    label = c.page.get_by_label("Account holder / label", exact=True)
    label.click()
    label.press_sequentially("Demo Landlord (new bank)", delay=45)
    c.wait(1)
    c.click("Review this change")
    c.see("Confirm: future rent")
    c.wait(2)
    c.click("Yes, confirm the account change")
    c.heading("Agent activity")
    c.click("Run the agent now")
    c.see("Payment completed")
    c.wait(1.5)
    c.click("Dashboard")
    c.see("BANK-7788-NEW")
    c.wait(3)


CLIPS = {
    "welcome": clip_welcome,
    "approve": clip_approve,
    "pay-receipts-replay": clip_pay_receipts_replay,
    "photo-attack": clip_photo_attack,
    "attack-lab": clip_attack_lab,
    "lab-real-image": clip_lab_real_image,
    "account-change": clip_account_change,
}


def main() -> int:
    url = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    only = set(sys.argv[2:])
    OUT.mkdir(parents=True, exist_ok=True)
    raw = OUT / "_raw"
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for name, fn in CLIPS.items():
            if only and name not in only:
                continue
            context = browser.new_context(
                viewport=VIEWPORT, device_scale_factor=VIDEO["width"] / VIEWPORT["width"],
                record_video_dir=str(raw), record_video_size=VIDEO,
            )
            context.add_init_script(CURSOR_JS)
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            print(f"recording {name}…", flush=True)
            try:
                fn(Clip(page), url)
            finally:
                video_path = page.video.path() if page.video else None
                context.close()
            if video_path:
                shutil.move(video_path, OUT / f"{name}.webm")
            if errors:
                print(f"  page errors in {name}: {errors}", file=sys.stderr)
                return 1
        browser.close()
    shutil.rmtree(raw, ignore_errors=True)
    print(f"clips: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
