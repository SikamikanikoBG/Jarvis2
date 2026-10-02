// Two changes to pixel-agents' webview transport (webview-ui/src/transport/index.ts) so it runs
// inside Jarvis:
//   1. its socket is /pixel-office/ws, next to the page, not /ws at the host root (Jarvis's own socket);
//   2. a click on a character (focusAgent) is also posted to the Jarvis page around the iframe,
//      which opens that agent's conversation.
// Fails loudly when an anchor is missing, so a bump of the pinned commit cannot half-apply.
import fs from 'node:fs';

const file = process.argv[2];
let src = fs.readFileSync(file, 'utf8');

function replaceOnce(anchor, replacement) {
  if (!src.includes(anchor)) {
    console.error(`patch-transport: anchor not found in ${file}:\n${anchor}`);
    process.exit(1);
  }
  src = src.replace(anchor, replacement);
}

replaceOnce(
  '${window.location.host}/ws${',
  "${window.location.host}${new URL('ws', window.location.href).pathname}${",
);

replaceOnce(
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
);

fs.writeFileSync(file, src);
console.log(`patch-transport: patched ${file}`);
