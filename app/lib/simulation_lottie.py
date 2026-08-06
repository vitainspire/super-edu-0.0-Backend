"""Lottie celebrations for generated simulations. Mirrors
frontend/lib/simulation-lottie.ts — the model NEVER writes Lottie JSON itself
(dense declarative math that fails silently when malformed); it only ever
calls playLottie('name'), a function this module injects into the page after
generation. Assets are the same 4 pre-built animations, vendored under
app/assets/lottie/ instead of re-generating anything.
"""
import json
import re
from pathlib import Path

_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets" / "lottie"

_LOTTIE_ASSETS: dict[str, dict] = {}
for _name in ("confetti", "star", "checkmark", "bounce"):
    with open(_ASSETS_DIR / f"{_name}.json", "r", encoding="utf-8") as _f:
        _LOTTIE_ASSETS[_name] = json.load(_f)

with open(_ASSETS_DIR / "lottie_light.min.js", "r", encoding="utf-8") as _f:
    _LOTTIE_PLAYER_SRC = _f.read()

LOTTIE_NAMES = list(_LOTTIE_ASSETS.keys())
SUGGESTED_CELEBRATION = "confetti" if "confetti" in LOTTIE_NAMES else LOTTIE_NAMES[0]


def _escape_for_script(text: str) -> str:
    return text.replace("</", "<\\/")


def used_lottie_names(html: str) -> list[str]:
    names: list[str] = []
    for m in re.finditer(r"""playLottie\s*\(\s*["'`]([^"'`]+)["'`]""", html):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _lottie_names_for(html: str) -> list[str]:
    if not LOTTIE_NAMES:
        return []
    all_calls = len(re.findall(r"\bplayLottie\s*\(", html))
    if not all_calls:
        return []
    literal_calls = len(re.findall(r"""\bplayLottie\s*\(\s*["'`]""", html))
    literal_names = [n for n in used_lottie_names(html) if n in LOTTIE_NAMES]
    if literal_calls < all_calls or not literal_names:
        return LOTTIE_NAMES
    return literal_names


_LOTTIE_RUNTIME = """
(function () {
  var REDUCE = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var DATA = window.__LOTTIE_DATA__ || {};
  var NAMES = Object.keys(DATA);
  var live = [];

  function report(message) {
    if (!document.body) return;
    var prev = document.body.getAttribute('data-lottie-report') || '';
    if (prev.indexOf(message) !== -1) return;
    document.body.setAttribute('data-lottie-report', prev ? prev + ' AND ' + message : message);
  }

  function hexToRgba(hex) {
    var h = String(hex).replace('#', '');
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    if (h.length !== 6) return null;
    var n = parseInt(h, 16);
    if (isNaN(n)) return null;
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255, 1];
  }

  function recolor(node, colour) {
    if (!node || typeof node !== 'object') return;
    if (Array.isArray(node)) {
      for (var i = 0; i < node.length; i++) recolor(node[i], colour);
      return;
    }
    if ((node.ty === 'fl' || node.ty === 'st') && node.c && node.c.a === 0) node.c.k = colour;
    for (var key in node) {
      if (Object.prototype.hasOwnProperty.call(node, key)) recolor(node[key], colour);
    }
  }

  function destroy(entry) {
    if (entry.done) return;
    entry.done = true;
    try { entry.anim.destroy(); } catch (e) {}
    if (entry.host.parentNode) entry.host.parentNode.removeChild(entry.host);
    var at = live.indexOf(entry);
    if (at !== -1) live.splice(at, 1);
  }

  window.playLottie = function (name, options) {
    options = options || {};
    var source = DATA[name];
    if (!source) {
      report('playLottie("' + name + '") was called but that animation does not exist. Available: ' + NAMES.join(', '));
      return null;
    }
    if (typeof lottie === 'undefined') {
      report('the bundled lottie player did not load');
      return null;
    }

    var data = source;
    if (options.color) {
      var colour = hexToRgba(options.color);
      if (colour) {
        data = JSON.parse(JSON.stringify(source));
        recolor(data, colour);
      }
    }

    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var size = options.size || Math.max(200, Math.min(380, Math.min(vw, vh) * 0.62));
    size = Math.min(size, vw, vh);

    var cx = vw / 2;
    var cy = vh / 2;
    if (options.target && typeof options.target.getBoundingClientRect === 'function') {
      var box = options.target.getBoundingClientRect();
      if (box.width || box.height) {
        cx = box.left + box.width / 2;
        cy = box.top + box.height / 2;
      }
    }

    var host = document.createElement('div');
    host.setAttribute('data-lottie-host', name);
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText =
      'position:fixed;pointer-events:none;z-index:9999;' +
      'left:' + Math.round(Math.max(0, Math.min(vw - size, cx - size / 2))) + 'px;' +
      'top:' + Math.round(Math.max(0, Math.min(vh - size, cy - size / 2))) + 'px;' +
      'width:' + Math.round(size) + 'px;height:' + Math.round(size) + 'px;';
    document.body.appendChild(host);

    while (live.length >= 3) destroy(live[0]);

    var anim = lottie.loadAnimation({
      container: host,
      renderer: 'svg',
      loop: false,
      autoplay: true,
      animationData: data
    });
    anim.setSpeed(REDUCE ? 2.4 : (options.speed || 1));

    var entry = { anim: anim, host: host, done: false };
    live.push(entry);
    anim.addEventListener('complete', function () { destroy(entry); });
    setTimeout(function () { destroy(entry); }, 8000);
    return anim;
  };
})();
"""


def inject_lottie(html: str) -> str:
    used = _lottie_names_for(html)
    if not used:
        return html

    data = {name: _LOTTIE_ASSETS[name] for name in used}
    bundle = (
        "<script>/* lottie-web 5.12.2 light build (MIT) — bundled so the page works offline */\n"
        f"{_escape_for_script(_LOTTIE_PLAYER_SRC)}\n</script>\n"
        f"<script>\nwindow.__LOTTIE_DATA__ = {_escape_for_script(json.dumps(data))};\n{_LOTTIE_RUNTIME}\n</script>\n"
    )

    # Plain string slicing, not re.sub — the replacement text is a giant
    # vendored JS blob full of literal backslashes, which re.sub would
    # misinterpret as backreferences (e.g. "\d") and raise re.error.
    head_match = re.search(r"</head>", html, re.I)
    if head_match:
        return html[:head_match.start()] + bundle + html[head_match.start():]
    body_match = re.search(r"<body[^>]*>", html, re.I)
    if body_match:
        return html[:body_match.end()] + "\n" + bundle + html[body_match.end():]
    return bundle + html


CELEBRATION_RULES = f"""

CELEBRATION ANIMATIONS
- A function playLottie(name, options) is already defined for you. Call it to celebrate.
- playLottie is GUARANTEED to exist before your script runs. You will not see its definition in your own output, and that is correct — it is added afterwards. Do not write your own version of it, a stub, a fallback, or a "typeof playLottie === 'undefined'" guard, and do not add a <script> tag, a library, or animation data for it. Just call it.
- These are the ONLY valid names: {', '.join(f"'{n}'" for n in LOTTIE_NAMES)}. Any other name does nothing.
- options is optional: {{ target: someElement, size: 300, color: '#ffcc00', speed: 1 }}. Pass target to play it centred over that element; leave it out to play in the middle of the screen. color repaints the animation, so skip it for 'confetti' unless you specifically want single-colour confetti.
- Example: playLottie('confetti') when the whole activity is finished, or playLottie('star', {{ target: tile }}) over the tile a child just got right.
- The animation is silent decoration drawn on top of the page. Keep your own written feedback ("Yay!" or "Correct!") as well — never rely on the animation alone to tell a child they were right. Text feedback is plain words only, no emoji.
- Never hand-write a CSS celebration: no @keyframes called sparkle, confetti, yay, celebrate, twinkle, hooray or anything similar. playLottie does that job better than a keyframe block can. CSS animation is still exactly right for two other things, so keep using it there: quick tap feedback (a transition on transform or opacity), and moving something to a position your JavaScript works out at run time."""
