// Custom-Transformer für xAI Grok über claude-code-router (Ersatz für den
// eingebauten "reasoning"-Transformer, CCR 2.0.0).
//
// Grok-Reasoning-Modelle denken von selbst und liefern die Denkspur als
// delta.reasoning_content im Stream. Dieser Transformer übersetzt sie in
// delta.thinking-Chunks (daraus macht der Anthropic-Ausgang des Routers echte
// thinking-Blöcke -> [DENKT] im Agenten-Journal).
//
// FIX gegenüber dem eingebauten "reasoning"-Transformer: dessen synthetisches
// "Denken abgeschlossen"-Event spreadet das auslösende Original-Delta mit -
// ist das auslösende Chunk ein tool_calls-Fragment, kommen die Tool-Argumente
// DOPPELT an ({"full":true}{"full":true}) und Claude Code lehnt den Tool-Call
// als kaputtes JSON ab (live reproduziert 29.08.2026, Igor-Journal 14:24).
// Hier trägt das synthetische Event AUSSCHLIESSLICH den thinking-Abschluss
// (content + signature); content/tool_calls fließen nur im nachgereichten
// Original-Event (mit index+1, damit der Anthropic-Ausgang einen neuen
// Content-Block öffnet - Verhalten identisch zum Original-Transformer).
// Außerdem entfällt dessen console.log pro Stream-Chunk.
module.exports = class GrokFix {
  constructor(options) {
    this.name = "grokfix";
    this.options = options || {};
  }

  async transformRequestIn(request) {
    if (request) {
      // Grok denkt ohne Ansteuerung; reasoning/thinking-Felder aus dem
      // Anthropic-Pfad würden xAI nur irritieren (400-Risiko).
      delete request.reasoning;
      delete request.thinking;
      delete request.enable_thinking;
    }
    return request;
  }

  async transformResponseOut(response) {
    var ctype = response.headers.get("Content-Type") || "";
    if (ctype.includes("application/json")) {
      var body = await response.json();
      var rc = body.choices && body.choices[0] && body.choices[0].message
        ? body.choices[0].message.reasoning_content : null;
      if (rc) {
        body.thinking = { content: rc };
        delete body.choices[0].message.reasoning_content;
      }
      return new Response(JSON.stringify(body), {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    }
    if (!ctype.includes("stream") || !response.body) {
      return response;
    }

    var decoder = new TextDecoder();
    var encoder = new TextEncoder();
    var reasoningBuf = "";
    var reasoningDone = false;
    var carry = "";
    var reader = response.body.getReader();

    function processLine(line, controller) {
      if (!(line.startsWith("data: ") && line.trim() !== "data: [DONE]")) {
        controller.enqueue(encoder.encode(line + "\n"));
        return;
      }
      var f;
      try {
        f = JSON.parse(line.slice(6));
      } catch (e) {
        controller.enqueue(encoder.encode(line + "\n"));
        return;
      }
      var delta = f.choices && f.choices[0] ? f.choices[0].delta : null;

      // 1) Denk-Chunk: als thinking-Delta weiterreichen und puffern.
      if (delta && delta.reasoning_content) {
        reasoningBuf += delta.reasoning_content;
        var g = Object.assign({}, f, {
          choices: [Object.assign({}, f.choices[0], {
            delta: Object.assign({}, delta, {
              thinking: { content: delta.reasoning_content },
            }),
          })],
        });
        delete g.choices[0].delta.reasoning_content;
        controller.enqueue(encoder.encode("data: " + JSON.stringify(g) + "\n\n"));
        return;
      }

      // 2) Erstes Nicht-Denk-Delta nach gepuffertem Denken: NUR den
      //    thinking-Abschluss (content + signature) synthetisieren -
      //    OHNE content/tool_calls (genau das war der Duplikat-Bug).
      if (delta && (delta.content || delta.tool_calls)
          && reasoningBuf && !reasoningDone) {
        reasoningDone = true;
        var done = Object.assign({}, f, {
          choices: [Object.assign({}, f.choices[0], {
            delta: {
              thinking: {
                content: reasoningBuf,
                signature: Date.now().toString(),
              },
            },
          })],
        });
        delete done.choices[0].finish_reason;
        controller.enqueue(encoder.encode("data: " + JSON.stringify(done) + "\n\n"));
      }

      if (delta && delta.reasoning_content) {
        delete delta.reasoning_content;
      }

      // 3) Original-Event weiterreichen; nach dem Denk-Abschluss index+1,
      //    damit der Anthropic-Ausgang einen NEUEN Block öffnet (Verhalten
      //    des Original-Transformers).
      if (delta && Object.keys(delta).length > 0) {
        if (reasoningDone) {
          f.choices[0].index++;
        }
        controller.enqueue(encoder.encode("data: " + JSON.stringify(f) + "\n\n"));
      }
    }

    var stream = new ReadableStream({
      async start(controller) {
        try {
          for (;;) {
            var r = await reader.read();
            if (r.done) {
              if (carry.trim()) {
                processLine(carry, controller);
              }
              break;
            }
            carry += decoder.decode(r.value, { stream: true });
            var lines = carry.split("\n");
            carry = lines.pop() || "";
            for (var i = 0; i < lines.length; i++) {
              if (lines[i].trim()) {
                try {
                  processLine(lines[i], controller);
                } catch (e) {
                  controller.enqueue(encoder.encode(lines[i] + "\n"));
                }
              }
            }
          }
        } catch (e) {
          controller.error(e);
        } finally {
          try { reader.releaseLock(); } catch (e2) {}
          controller.close();
        }
      },
    });
    return new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      },
    });
  }
};
