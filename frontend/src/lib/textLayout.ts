"use client";

// Text measurement with @chenglou/pretext: computes wrapped text height with
// canvas arithmetic instead of DOM reflow, so streamed report blocks and
// virtualized rows can reserve their exact height before they paint.
//
// Rules from the pretext README:
// - Browser only (needs Canvas 2D). Never call during server rendering.
// - Prepare once per text+font; on resize only re-run layout().
// - Wait for the web font to load, then clearCache(), before preparing.
// - Use px font sizes and a named family (not system-ui).
// - Painted text must use the same font, line-height and width, plus
//   `overflow-wrap: break-word`.

import { clearCache, layout, prepare, type PreparedText } from "@chenglou/pretext";
import { useEffect, useMemo, useState } from "react";

const cache = new Map<string, PreparedText>();

function prepared(text: string, font: string): PreparedText {
  const key = font + "\u0000" + text;
  let p = cache.get(key);
  if (!p) {
    p = prepare(text, font);
    cache.set(key, p);
  }
  return p;
}

const fontLoads = new Map<string, Promise<void>>();

// Resolves once `font` (a CSS font shorthand such as "15px KaTeX_Main") is loaded.
export function loadFont(font: string): Promise<void> {
  let p = fontLoads.get(font);
  if (!p) {
    p = document.fonts.load(font).then(() => {
      clearCache();
      cache.clear();
    });
    fontLoads.set(font, p);
  }
  return p;
}

export function measureHeight(text: string, font: string, width: number, lineHeight: number) {
  return layout(prepared(text, font), Math.floor(width), lineHeight);
}

// Height of `text` at `width`, or null until the font has loaded.
export function useTextHeight(text: string, font: string, width: number, lineHeight: number): number | null {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    let live = true;
    loadFont(font).then(() => live && setReady(true));
    return () => {
      live = false;
    };
  }, [font]);
  return useMemo(
    () => (ready && width > 0 ? measureHeight(text, font, width, lineHeight).height : null),
    [ready, text, font, width, lineHeight],
  );
}
