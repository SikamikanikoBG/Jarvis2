// Changes to pixel-agents' webview so it runs inside Jarvis. Run from the checkout's root.
//   transport/index.ts:
//     1. its socket is /pixel-office/ws, next to the page, not /ws at the host root (Jarvis's own);
//     2. a click on a character (focusAgent) is also posted to the Jarvis page around the iframe,
//        which opens that agent's conversation.
//   office/components/ToolOverlay.tsx:
//     3. a character with nothing running says "Done" (its run has ended) rather than "Idle" —
//        Jarvis keeps finished runs in the office for a while, labels always on.
//   Phones (the office is mostly watched from one):
//     4. hooks/useEditorActions.ts: the first layout is zoomed to fit the screen. The default
//        (2 x devicePixelRatio) is twice a phone's width; on a desktop it already fits.
//     5. office/components/OfficeCanvas.tsx: one finger pans, two fingers pinch-zoom (it only
//        knew the mouse). A tap still clicks.
// Fails loudly when an anchor is missing, so a bump of the pinned commit cannot half-apply.
import fs from 'node:fs';

function patch(file, edits) {
  let src = fs.readFileSync(file, 'utf8');
  for (const [anchor, replacement] of edits) {
    if (!src.includes(anchor)) {
      console.error(`patch-webview: anchor not found in ${file}:\n${anchor}`);
      process.exit(1);
    }
    src = src.replace(anchor, replacement);
  }
  fs.writeFileSync(file, src);
  console.log(`patch-webview: patched ${file}`);
}

patch('webview-ui/src/transport/index.ts', [
  [
    '${window.location.host}/ws${',
    "${window.location.host}${new URL('ws', window.location.href).pathname}${",
  ],
  [
    '  ws.connect();\n',
    `  ws.connect();
  // Jarvis: tell the page around the iframe which character was clicked.
  const sendToServer = ws.send.bind(ws);
  ws.send = (message: Parameters<typeof sendToServer>[0]) => {
    if (message.type === 'focusAgent' && window.parent !== window) {
      window.parent.postMessage(
        { source: 'pixel-office', type: 'focusAgent', id: message.id },
        window.location.origin,
      );
    }
    sendToServer(message);
  };
`,
  ],
]);

patch('webview-ui/src/office/components/ToolOverlay.tsx', [
  ["  return 'Idle';\n}", "  return isActive ? 'Working' : 'Done';\n}"],
]);

patch('webview-ui/src/hooks/useEditorActions.ts', [
  [
    `  const setLastSavedLayout = useCallback((layout: OfficeLayout) => {
    lastSavedLayoutRef.current = structuredClone(layout);
  }, []);`,
    `  // Jarvis: the first layout is zoomed to fit the screen (never above the default).
  const fittedRef = useRef(false);
  const setLastSavedLayout = useCallback((layout: OfficeLayout) => {
    lastSavedLayoutRef.current = structuredClone(layout);
    if (!fittedRef.current && layout.cols > 0 && layout.rows > 0) {
      fittedRef.current = true;
      const dpr = window.devicePixelRatio || 1;
      const fit = Math.floor(
        Math.min(
          (window.innerWidth * dpr) / (layout.cols * 16),
          (window.innerHeight * dpr) / (layout.rows * 16),
        ),
      );
      setZoom((current) => Math.max(ZOOM_MIN, Math.min(current, fit)));
    }
  }, []);`,
  ],
]);

patch('webview-ui/src/office/components/OfficeCanvas.tsx', [
  [
    '  // Zoom scroll accumulator for trackpad pinch sensitivity\n',
    `  // Jarvis: touch gesture state (pan start, pinch start)
  const touchRef = useRef<{
    x: number;
    y: number;
    panX: number;
    panY: number;
    dist: number;
    zoom: number;
    moved: boolean;
  } | null>(null);
  // Zoom scroll accumulator for trackpad pinch sensitivity
`,
  ],
  [
    `    return () => canvas.removeEventListener('wheel', handleWheel);
  }, [handleWheel]);
`,
    `    return () => canvas.removeEventListener('wheel', handleWheel);
  }, [handleWheel]);

  // Jarvis: touch. One finger pans, two pinch-zoom; a tap (no movement) still becomes a click.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    canvas.style.touchAction = 'none';
    const spread = (t: TouchList) =>
      Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const begin = (t: TouchList, moved: boolean) => {
      touchRef.current = {
        x: t[0].clientX,
        y: t[0].clientY,
        panX: panRef.current.x,
        panY: panRef.current.y,
        dist: t.length > 1 ? spread(t) : 0,
        zoom,
        moved,
      };
    };
    const onStart = (e: TouchEvent) => begin(e.touches, e.touches.length > 1);
    const onMove = (e: TouchEvent) => {
      const s = touchRef.current;
      if (!s) return;
      const t = e.touches;
      if (t.length > 1) {
        e.preventDefault();
        if (!s.dist) s.dist = spread(t);
        s.moved = true;
        const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round((s.zoom * spread(t)) / s.dist)));
        if (next !== zoom) onZoomChange(next);
        return;
      }
      const dpr = window.devicePixelRatio || 1;
      const dx = (t[0].clientX - s.x) * dpr;
      const dy = (t[0].clientY - s.y) * dpr;
      if (!s.moved && Math.hypot(dx, dy) < 8 * dpr) return;
      e.preventDefault();
      s.moved = true;
      officeState.cameraFollowId = null;
      officeState.cancelGreeterCamera();
      panRef.current = clampPan(s.panX + dx, s.panY + dy);
    };
    const onEnd = (e: TouchEvent) => {
      // Lifting one finger of a pinch carries on as a pan from where the other one is.
      if (e.touches.length > 0) begin(e.touches, true);
      else touchRef.current = null;
    };
    canvas.addEventListener('touchstart', onStart, { passive: true });
    canvas.addEventListener('touchmove', onMove, { passive: false });
    canvas.addEventListener('touchend', onEnd);
    canvas.addEventListener('touchcancel', onEnd);
    return () => {
      canvas.removeEventListener('touchstart', onStart);
      canvas.removeEventListener('touchmove', onMove);
      canvas.removeEventListener('touchend', onEnd);
      canvas.removeEventListener('touchcancel', onEnd);
    };
  }, [zoom, onZoomChange, officeState, panRef, clampPan]);
`,
  ],
]);
