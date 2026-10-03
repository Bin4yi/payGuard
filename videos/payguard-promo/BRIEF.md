---
workflow: general-video
flow: automation
storyboard: no
message: "The AI proposes, only you approve: PayGuard makes it impossible for a fooled agent to pay the wrong person, or pay twice."
destination: youtube
aspect: 1920x1080
language: en
audience: hackathon judges and technical viewers
length: under 3 min (user, mid-build)
narration: yes
---

## Intent

A 3–5 minute, Apple-level promo and technical explainer for PayGuard (the app in
this repo). It explains the full technical breakthrough, covers every feature,
and uses diagrams plus real app footage. User's words: "make this apple level
promo video. explain the full technical breakthrough of this app. use diagrams
and real footages also. include all the features in this app."

## Assets

- ../../docs/demo/footage/*.webm — real 1080p app footage recorded with Playwright (record_footage.py)
- ../../fixtures/real_notices/* — real scam/notice images from Wikimedia Commons (see CREDITS.md)
- ../../web/public/samples/poisoned-rent-notice.png — printed poisoned notice used in the photo demo

## Customizations

- Style: Neumorphism + Glassmorphism (user request).
- AI voiceover + music bed (user chose "AI voiceover + music").
- New app features filmed: photo notice upload (OCR) and the Attack Lab.

## Notes

- Not signed in to HeyGen: voice = local Kokoro, music = local MusicGen (offline engines).
- Only claim what the app actually does; numbers come from real runs (Laya 9/10, 10/10 stopped, 216 tests).
- The agent in the demo is a deterministic stand-in, not live Gemini; say "AI agent", not "Gemini did this".
