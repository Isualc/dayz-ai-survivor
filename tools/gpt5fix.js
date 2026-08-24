// Custom-Transformer fuer OpenAI GPT-5-Modelle ueber claude-code-router.
// Behebt die Inkompatibilitaeten der GPT-5-Familie mit dem OpenAI-Endpoint:
//  1. max_tokens -> max_completion_tokens  (GPT-5 lehnt max_tokens mit 400 ab)
//  2. reasoning / thinking ENTFERNEN (chat/completions kennt kein
//     reasoning-Objekt -> 400 "Unknown parameter").
//  3. gpt-5.6*: reasoning_effort EXPLIZIT auf "none" setzen. Nur loeschen
//     reicht nicht mehr - OpenAI wendet sonst einen Reasoning-Default an und
//     lehnt Function-Tools ueber /v1/chat/completions mit 400 ab
//     ("Function tools with reasoning_effort are not supported ...
//       set reasoning_effort to 'none'"). Aeltere 5.4/5.5 kennen "none"
//     nicht -> dort weiterhin loeschen.
module.exports = class Gpt5Fix {
  constructor(options) {
    this.name = "gpt5fix";
    this.options = options || {};
  }

  async transformRequestIn(request, provider) {
    if (request) {
      if (request.max_tokens != null) {
        request.max_completion_tokens = request.max_tokens;
        delete request.max_tokens;
      }
      delete request.reasoning;
      delete request.thinking;
      var model = String(request.model || "");
      if (/^gpt-5\.[6-9]/.test(model) || /^gpt-[6-9]/.test(model)) {
        request.reasoning_effort = "none";
      } else {
        delete request.reasoning_effort;
      }
    }
    return request;
  }
};
