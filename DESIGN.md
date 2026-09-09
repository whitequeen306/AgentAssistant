---
name: Liquid Frost
description: >
  A frosted-glass desktop assistant card. Translucent surfaces with backdrop
  blur, soft page morphs, and soft hover deformation on interactive regions.
  The window chrome around the card stays transparent (pywebview); the card
  itself is liquid glass — readable panels, calm blue accent, motion that
  feels springy but never noisy.
colors:
  primary: "#1A1A2E"
  secondary: "#555555"
  tertiary: "#8A8A8E"
  accent: "#007AFF"
  accent-strong: "#0066D1"
  accent-contrast: "#FFFFFF"
  accent-soft: "#E8F1FF"
  surface: "#FFFFFF"
  surface-elevated: "#F2F2F5"
  surface-sunken: "#F7F7F9"
  border: "#E5E5EA"
  border-strong: "#D1D1D6"
  bubble-user-bg: "#0066D1"
  bubble-user-text: "#FFFFFF"
  bubble-assistant-bg: "#F2F2F5"
  bubble-assistant-border: "#E5E5EA"
  success: "#1F9D4D"
  warning: "#B7790A"
  error: "#D32F2F"
  dot-idle: "#8A8A8E"
  dot-thinking: "#B7790A"
  dot-listening: "#1F9D4D"
  overlay: "rgba(0, 0, 0, 0.32)"
typography:
  display:
    fontFamily: "Segoe UI Variable Display, -apple-system, system-ui, sans-serif"
    fontSize: 15px
    fontWeight: 600
    letterSpacing: -0.01em
  body:
    fontFamily: "Segoe UI Variable Text, -apple-system, system-ui, sans-serif"
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.5
  caption:
    fontFamily: "Segoe UI Variable Text, -apple-system, system-ui, sans-serif"
    fontSize: 11px
    fontWeight: 500
    letterSpacing: 0.02em
  mono:
    fontFamily: "Cascadia Code, Consolas, monospace"
    fontSize: 11px
    fontWeight: 400
rounded:
  xs: 6px
  sm: 8px
  md: 10px
  lg: 14px
  xl: 16px
  pill: 9999px
spacing:
  xs: 4px
  sm: 8px
  md: 12px
  lg: 16px
  xl: 24px
  2xl: 32px
components:
  button-primary:
    backgroundColor: "{colors.accent-strong}"
    textColor: "{colors.accent-contrast}"
    rounded: "{rounded.md}"
    padding: 8px 16px
  button-primary-hover:
    backgroundColor: "{colors.accent-strong}"
  button-ghost:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    rounded: "{rounded.md}"
    padding: 8px 12px
  button-icon:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.secondary}"
    rounded: "{rounded.sm}"
    size: 28px
  button-icon-hover:
    backgroundColor: "{colors.surface-elevated}"
    textColor: "{colors.primary}"
  input:
    backgroundColor: "{colors.surface-sunken}"
    textColor: "{colors.primary}"
    rounded: "{rounded.sm}"
    padding: 8px 12px
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.xl}"
    padding: 0px
  pill:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.primary}"
    rounded: "{rounded.pill}"
    padding: 0px
  bubble-user:
    backgroundColor: "{colors.bubble-user-bg}"
    textColor: "{colors.bubble-user-text}"
    rounded: "{rounded.lg}"
    padding: 8px 12px
  bubble-assistant:
    backgroundColor: "{colors.bubble-assistant-bg}"
    textColor: "{colors.primary}"
    rounded: "{rounded.lg}"
    padding: 8px 12px
  nav-item-active:
    backgroundColor: "{colors.accent-soft}"
    textColor: "{colors.accent-strong}"
    rounded: "{rounded.md}"
---

## Overview

**Quiet Card** — the opposite of a showpiece. The assistant is a single
opaque card that floats above the desktop (the window is transparent; only the
card is solid). It morphs between four discrete sizes — a small ask box when
idle, a search bar docked to the top edge, a hairline handle at the side, and
the full sidebar + content panel. The surface is matte, the shadow is soft,
the corners are rounded, and there is exactly one interactive color.

The aesthetic draws from calm native utility apps (Settings, Notes, Things) —
not from liquid-glass or visionOS. Nothing sparkles, nothing refracts, nothing
overshoots. The card communicates state through position, size, and a single
status dot — not through optical effects.

**Signature moment:** the morph — the card stretches from the idle pill into
the full panel with a plain ease (no spring overshoot, no glass thickening).
The restraint IS the identity: a quiet, precise resize that feels like a native
window snapping into place. No second effect competes with it.

## Colors

Light-first, opaque. The card is solid white; secondary surfaces sit one step
darker; inputs sit slightly sunken. Borders are hairline grays.

- **Primary (#1A1A2E):** Ink for headings and primary text.
- **Secondary (#555555):** Body secondary text, captions.
- **Tertiary (#8A8A8E):** Placeholders, timestamps, disabled.
- **Accent (#007AFF):** The sole interactive hue — focus rings, active nav,
  links. Never used as a solid fill with white text (use accent-strong for that).
- **Accent-strong (#0066D1):** A hair deeper blue for solid fills (primary
  buttons, user bubbles) so white text clears WCAG AA on the surface.
- **Accent-soft (#E8F1FF):** Tinted background for the active nav item.
- **Surface (#FFFFFF):** The card body.
- **Surface-elevated (#F2F2F5):** Hover states, assistant bubbles, raised rows.
- **Surface-sunken (#F7F7F9):** Input fields — receded from the card surface.
- **Border (#E5E5EA):** Hairline dividers and input borders.
- **Success / Warning / Error:** Desaturated, everyday-safe — not neon.
- **Dot idle/thinking/listening:** muted gray / amber / green status indicators.

Dark theme (applied via `.dark`) mirrors the light set onto iOS-dark material:
surface #1C1C1E, surface-elevated #2C2C2E, surface-sunken #111114, border
#38383A, primary #F2F2F7, secondary #AEAEB2, tertiary #636366, accent #0A84FF,
accent-strong #0A84FF, accent-soft #0B2A4A, bubble-assistant-bg #2C2C2E. All
opacity values are solid (no alpha-on-alpha glass) for legibility.

## Typography

System-native stack for zero-latency rendering and OS-level font smoothing on
Windows (Segoe UI Variable). No web fonts are loaded.

- **Display (15px/600):** Page titles, sidebar headers. Tight tracking.
- **Body (13px/400, 1.5 line-height):** Chat messages. Generous line-height.
- **Caption (11px/500):** Tool status, timestamps. Slightly tracked.
- **Mono (11px):** Tool-call names, code snippets, parameter keys.

## Layout

A floating card with four discrete sizes: pill 340×56, docked-search ~40% screen
width × 48, sliver 12×160, and main ~40% screen width × 560. Main is a
sidebar (240px, collapses to a 56px rail on non-chat pages) + content area.
Internal spacing follows a 4px grid. The card has 16px corner radius; sub-
elements use smaller radii (buttons 10, inputs 8, bubbles 14).

## Elevation & Depth

Depth is conveyed by a single soft shadow, not layered optics. No backdrop
blur, no specular highlight, no refraction tint, no inner glow.

1. **Drop shadow** — `0 6px 20px rgba(0,0,0,0.12)` on the card only; calm.
2. **Hairline border** — 1px `--border` on cards, inputs, bubbles.
3. **Surface offset** — elevation expressed as a one-step darker fill, not a
   blur layer. Hovered rows step surface → surface-elevated.
4. **Overlay** — modals dim the card with a flat `rgba(0,0,0,0.32)` scrim.

## Shapes

Calm and consistent. The idle pill is a stadium (pill radius); the card is a
soft 16px rectangle; bubbles are 14px; buttons 10px; inputs 8px. No sharp
corners, but no bubbly pebbles either — the radii are modest and reserved.

## Components

All components are solid fills on the card surface. Interactive states
communicate through fill stepping (surface → surface-elevated on hover) and the
single accent (focus ring, active nav, primary buttons). Icon buttons are
transparent by default and gain an elevated fill on hover — they are never
glassy. Toggles use accent-strong when on. Status dots are 8px solid circles
that shift color + a gentle opacity pulse while thinking/listening.

Icons are a single unified inline-SVG set (lucide). No emoji, no decorative
symbols, no pictographic characters appear anywhere in the UI — text labels
and SVG glyphs only.

## Do's and Don'ts

- **Do** keep every surface opaque — no `backdrop-filter`, no translucency on
  the card. Only the window body around the card is transparent.
- **Do** use the single accent for all interactive emphasis; reserve
  accent-strong for solid fills carrying white text.
- **Do** animate only `transform` and `opacity`; use plain ease curves
  (`cubic-bezier(0.4, 0, 0.2, 1)`) — no spring overshoot.
- **Do** honor `prefers-reduced-motion`: skip the morph, show content
  instantly, never hide-by-default.
- **Don't** use emoji or decorative symbol characters — replace with lucide
  SVG icons or plain text labels.
- **Don't** add more than one accent color. Blue is the only interactive hue.
- **Don't** use box-shadow heavier than the defined drop shadow.
- **Don't** introduce borders thicker than 1px.
