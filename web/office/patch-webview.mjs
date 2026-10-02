// Changes to pixel-agents' webview so it runs inside Jarvis. Run from the checkout's root.
//   transport/index.ts:
//     1. its socket is /pixel-office/ws, next to the page, not /ws at the host root (Jarvis's own);
//     2. a click on a character (focusAgent) is also posted to the Jarvis page around the iframe,
//        which opens that agent's conversation.
//   office/components/ToolOverlay.tsx:
//     3. a character with nothing running says "Done" (its run has ended) rather than "Idle" —
//        Jarvis keeps finished runs in the office for a while, labels always on.
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
